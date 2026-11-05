"""Knowledge base: manifest, loaders, chunking, embedding batching, and the Milvus build /
activate / search / rollback cycle. Milvus runs on Milvus Lite (embedded) by default and
additionally against a server when MILVUS_TEST_URI is set (e.g. http://127.0.0.1:19530)."""

import json
import os
import shutil
import uuid
from pathlib import Path

import httpx
import pytest

from industrial_ai.integrations.embedding_client import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    batches,
)
from industrial_ai.knowledge.builder import BuildError, build_knowledge_base
from industrial_ai.knowledge.chunker import chunk_document, split_text
from industrial_ai.knowledge.loaders import load_markdown
from industrial_ai.knowledge.manifest import (
    DocumentEntry,
    ManifestError,
    load_manifest,
    verify_documents,
)
from industrial_ai.knowledge.milvus_store import MilvusKnowledgeStore
from industrial_ai.knowledge.retriever import KnowledgeBaseIncompatible, KnowledgeRetriever

from .fakes import FakeEmbedder

SAMPLE = Path(__file__).parent.parent / "examples" / "knowledge"
MILVUS_URI = os.environ.get("MILVUS_TEST_URI")


# ---- manifest / loaders / chunking -------------------------------------------------------

def test_sample_manifest_verifies():
    manifest, sha = load_manifest(SAMPLE / "manifest.json")
    files = verify_documents(manifest, SAMPLE)
    assert len(files) == 4 and len(sha) == 64


def test_tampered_document_is_rejected(tmp_path):
    shutil.copytree(SAMPLE, tmp_path / "kb")
    (tmp_path / "kb" / "docs" / "health_levels.md").write_text("tampered", encoding="utf-8")
    manifest, _ = load_manifest(tmp_path / "kb" / "manifest.json")
    with pytest.raises(ManifestError, match="health-levels: sha256 mismatch"):
        verify_documents(manifest, tmp_path / "kb")


def test_path_escape_is_rejected(tmp_path):
    (tmp_path / "secret.md").write_text("x")
    (tmp_path / "kb").mkdir()
    manifest_path = tmp_path / "kb" / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "v1", "documents": [
        {"doc_id": "a", "path": "../secret.md", "title": "t", "sha256": "0" * 64}]}))
    manifest, _ = load_manifest(manifest_path)
    with pytest.raises(ManifestError, match="escapes"):
        verify_documents(manifest, tmp_path / "kb")


def test_markdown_sections_keep_heading_path(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("# 标题\n引言\n## 一\n内容一\n### 1.1\n细节\n## 二\n内容二\n"
                  "```\n# 不是标题\n```\n", encoding="utf-8")
    sections = load_markdown(md)
    assert [(s.heading, s.text.splitlines()[0]) for s in sections] == [
        ("标题", "引言"), ("标题 > 一", "内容一"), ("标题 > 一 > 1.1", "细节"),
        ("标题 > 二", "内容二")]
    assert "# 不是标题" in sections[-1].text


def test_split_text_respects_size_and_overlap():
    text = "".join(f"第{i}句话内容比较长一些。" for i in range(40))
    chunks = split_text(text, max_chars=60, overlap=15)
    assert all(len(c) <= 60 for c in chunks) and len(chunks) > 5
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a[-12:] in b  # trailing sentence repeated at the start of the next chunk


def test_chunk_ids_and_metadata():
    doc = DocumentEntry(doc_id="d1", path="x.md", title="T", sha256="0" * 64,
                        access_scope="radiation_protection")
    sections = load_markdown(SAMPLE / "docs" / "health_levels.md")
    chunks = chunk_document(doc, sections, max_chars=120, overlap=20)
    assert chunks[0].chunk_id == "d1#00000" and chunks[-1].chunk_index == len(chunks) - 1
    assert {c.access_scope for c in chunks} == {"radiation_protection"}
    assert chunks[0].embedding_text().startswith("T / ")


# ---- embedding client --------------------------------------------------------------------

def test_batches_bound_count_and_chars():
    texts = ["a" * 10] * 5 + ["b" * 50]
    assert [(s, len(b)) for s, b in batches(texts, max_items=2, max_chars=1000)] == \
        [(0, 2), (2, 2), (4, 2)]
    assert [(s, len(b)) for s, b in batches(texts, max_items=64, max_chars=55)] == \
        [(0, 5), (5, 1)]


@pytest.mark.anyio
async def test_embedding_client_glm_shape():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, request.headers["authorization"], body))
        data = [{"index": i, "embedding": [float(i)] * 4} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": list(reversed(data)), "model": body["model"]})

    config = EmbeddingConfig(base_url="https://open.bigmodel.cn/api/paas/v4",
                             model="embedding-3", dimensions=4, api_key="key", batch_size=2)
    client = EmbeddingClient(config, transport=httpx.MockTransport(handler))
    vectors = await client.embed(["一", "二", "三"])
    assert vectors == [[0.0] * 4, [1.0] * 4, [0.0] * 4]  # ordered by index within each batch
    assert seen[0][0] == "/api/paas/v4/embeddings" and seen[0][1] == "Bearer key"
    assert seen[0][2] == {"model": "embedding-3", "input": ["一", "二"], "dimensions": 4}
    await client.aclose()


@pytest.mark.anyio
async def test_embedding_dimension_mismatch_is_fatal():
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 8}]})

    client = EmbeddingClient(EmbeddingConfig(base_url="http://e", model="m", dimensions=4,
                                             retry_backoff_seconds=0),
                             transport=httpx.MockTransport(handler))
    with pytest.raises(EmbeddingError, match="4-dim"):
        await client.embed(["x"])


# ---- Milvus build / activate / search / rollback -----------------------------------------

@pytest.fixture(params=["lite", "server"])
def store(request, tmp_path):
    if request.param == "lite":
        uri, index = str(tmp_path / "milvus_lite.db"), "FLAT"
    else:
        if not MILVUS_URI:
            pytest.skip("MILVUS_TEST_URI not set")
        uri, index = MILVUS_URI, "HNSW"
    s = MilvusKnowledgeStore(uri, alias=f"test_kb_{uuid.uuid4().hex[:8]}", index_type=index)
    yield s
    try:
        s.client.drop_alias(s.alias)  # aliases must go before their collections
    except Exception:
        pass
    for v in s.versions():
        s.client.drop_collection(s.collection_name(v["version"]))
    s.close()


def _manifest_copy(tmp_path: Path, version: str) -> Path:
    target = tmp_path / f"kb_{version}_{uuid.uuid4().hex[:6]}"
    shutil.copytree(SAMPLE, target)
    path = target / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _retriever(store, embedder, **kw) -> KnowledgeRetriever:
    return KnowledgeRetriever(store, embedder, pinned_version=kw.get("pinned"), top_k=3,
                              min_score=0.05, allowed_classifications=["public", "internal"])


@pytest.mark.anyio
async def test_build_search_filter_and_rollback(store, tmp_path):
    embedder = FakeEmbedder()
    r1 = await build_knowledge_base(_manifest_copy(tmp_path, "v1"), store, embedder,
                                    chunk_chars=200, chunk_overlap=30)
    assert r1.activated and r1.previous_version is None and r1.chunks >= 4
    assert store.active_version() == "v1"
    assert store.metadata("v1")["embedding"] == {"model": "fake-glm", "dimensions": 128}

    retriever = _retriever(store, embedder)
    result = await retriever.search("mSv/h 与 µSv/h 怎么换算")
    assert result.kb_version == "v1"
    assert result.hits[0].doc_id == "dose-rate-units"
    assert result.hits[0].section and result.hits[0].score > 0

    # Role-restricted document only for users with the role.
    query = "受限文档 radiation_protection 角色 过滤"
    plain = await retriever.search(query, top_k=10)
    assert "restricted-sample" not in {h.doc_id for h in plain.hits}
    privileged = await retriever.search(query, top_k=10, roles=("radiation_protection",))
    assert "restricted-sample" in {h.doc_id for h in privileged.hits}

    # New release, then rollback; the old collection is kept.
    r2 = await build_knowledge_base(_manifest_copy(tmp_path, "v2"), store, embedder,
                                    chunk_chars=200, chunk_overlap=30)
    assert r2.previous_version == "v1" and store.active_version() == "v2"
    assert (await retriever.search("健康等级")).kb_version == "v2"
    assert (await retriever.search("健康等级", version="v1")).kb_version == "v1"
    assert store.activate("v1") == "v2" and store.active_version() == "v1"
    assert {v["version"] for v in store.versions()} == {"v1", "v2"}

    with pytest.raises(ValueError, match="active"):
        store.drop("v1")
    with pytest.raises(BuildError, match="already exists"):
        await build_knowledge_base(_manifest_copy(tmp_path, "v2"), store, embedder,
                                   chunk_chars=200, chunk_overlap=30)


@pytest.mark.anyio
async def test_build_without_activation_keeps_current(store, tmp_path):
    embedder = FakeEmbedder()
    await build_knowledge_base(_manifest_copy(tmp_path, "v1"), store, embedder,
                               chunk_chars=200, chunk_overlap=30)
    report = await build_knowledge_base(_manifest_copy(tmp_path, "v2"), store, embedder,
                                        chunk_chars=200, chunk_overlap=30, activate=False)
    assert not report.activated and store.active_version() == "v1"


@pytest.mark.anyio
async def test_query_embedder_must_match_index(store, tmp_path):
    await build_knowledge_base(_manifest_copy(tmp_path, "v1"), store, FakeEmbedder(dims=128),
                               chunk_chars=200, chunk_overlap=30)
    for other in (FakeEmbedder(dims=64), FakeEmbedder(dims=128, model="embedding-3")):
        retriever = _retriever(store, other)
        with pytest.raises(KnowledgeBaseIncompatible):
            await retriever.search("剂量率")
        assert (await retriever.status())["status"] == "incompatible"


@pytest.mark.anyio
async def test_failed_build_leaves_no_partial_collection(store, tmp_path):
    class BrokenEmbedder(FakeEmbedder):
        async def embed(self, texts):  # vectors that cannot find themselves
            return [[1.0] + [0.0] * 127 for _ in texts]

    with pytest.raises(BuildError, match="self-retrieval"):
        await build_knowledge_base(_manifest_copy(tmp_path, "v1"), store, BrokenEmbedder(),
                                   chunk_chars=200, chunk_overlap=30)
    assert store.versions() == [] and store.active_version() is None
