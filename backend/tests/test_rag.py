"""GLM embeddings client, chunking, and the Milvus build/activate/search/rollback cycle
(Milvus Lite always; a server too when MILVUS_TEST_URI is set)."""

import json
import os
import shutil
import uuid

import httpx
import pytest

from app.rag.embeddings import GLMEmbeddings
from app.rag.ingest import build, load_manifest, split, verify
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore, KnowledgeUnavailable

from .conftest import KNOWLEDGE, FakeEmbeddings


def test_glm_request_shape_and_batching():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append((req.url.path, req.headers.get("authorization"), body))
        data = [{"index": i, "embedding": [float(i)] * 4} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": list(reversed(data))})

    emb = GLMEmbeddings(base_url="https://open.bigmodel.cn/api/paas/v4", model="embedding-3",
                        dimensions=4, api_key="k", batch_size=2,
                        transport=httpx.MockTransport(handler))
    assert emb.embed_documents(["一", "二", "三"]) == [[0.0] * 4, [1.0] * 4, [0.0] * 4]
    assert seen[0] == ("/api/paas/v4/embeddings", "Bearer k",
                       {"model": "embedding-3", "input": ["一", "二"], "dimensions": 4})


def test_wrong_dimensions_rejected():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 8}]}))
    emb = GLMEmbeddings(base_url="http://e", model="m", dimensions=4, transport=transport)
    with pytest.raises(RuntimeError, match="4 dimensions"):
        emb.embed_query("x")


def test_manifest_checksums_and_markdown_sections(tmp_path):
    m = load_manifest(KNOWLEDGE)
    files = verify(m, KNOWLEDGE)
    entry = next(d for d in m.documents if d.doc_id == "dose-rate-units")
    chunks = split(entry, files["dose-rate-units"], 200, 30)
    sections = {c.metadata["section"] for c in chunks}
    assert "剂量率单位与换算（示例知识文档） > 2 换算关系" in sections
    shutil.copytree(KNOWLEDGE, tmp_path / "kb")
    (tmp_path / "kb" / "docs" / "health_levels.md").write_text("tampered")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        verify(load_manifest(tmp_path / "kb"), tmp_path / "kb")


def test_corpus_without_manifest(tmp_path):
    (tmp_path / "a.md").write_text("# 标题\n内容一。内容二。", encoding="utf-8")
    m = load_manifest(tmp_path)
    assert m.version.startswith("v") and m.documents[0].doc_id == "a.md"


@pytest.fixture(params=["lite", "server"])
def store(request, tmp_path):
    if request.param == "lite":
        pytest.importorskip("milvus_lite", reason="Milvus Lite has no Windows build")
        s = KnowledgeStore(str(tmp_path / "kb.db"), f"kb_{uuid.uuid4().hex[:6]}", index_type="FLAT")
    else:
        if not os.environ.get("MILVUS_TEST_URI"):
            pytest.skip("MILVUS_TEST_URI not set")
        s = KnowledgeStore(os.environ["MILVUS_TEST_URI"], f"test_{uuid.uuid4().hex[:6]}")
    yield s
    try:
        s.client.drop_alias(s.name)
    except Exception:
        pass
    for v in s.versions():
        s.client.drop_collection(s.collection(v["version"]))
    s.close()


@pytest.mark.anyio
async def test_build_search_rollback_and_compatibility(store):
    emb = FakeEmbeddings()
    r1 = build(KNOWLEDGE, store, emb, chunk_size=300, overlap=40, version="v1")
    assert r1.activated and r1.previous_version is None and store.active_version() == "v1"
    retriever = Retriever(store, emb, top_k=3, min_score=0.05)
    version, hits = await retriever.search("mGy/h 与 µGy/h 怎么换算")
    assert version == "v1" and hits[0].doc_id == "dose-rate-units"
    # access_scope != "all" is never returned (no user roles yet)
    _, hits = await retriever.search("受限文档 radiation_protection 角色 过滤", top_k=10)
    assert "restricted-sample" not in {h.doc_id for h in hits}

    r2 = build(KNOWLEDGE, store, emb, chunk_size=300, overlap=40, version="v2")
    assert r2.previous_version == "v1" and store.active_version() == "v2"
    assert (await retriever.search("健康等级", version="v1"))[0] == "v1"
    assert store.activate("v1") == "v2"
    with pytest.raises(ValueError, match="active"):
        store.drop("v1")
    with pytest.raises(ValueError, match="already exists"):
        build(KNOWLEDGE, store, emb, version="v2")

    other = Retriever(store, FakeEmbeddings(dimensions=64), top_k=3)
    with pytest.raises(KnowledgeUnavailable, match="rebuild"):
        await other.search("剂量率")
    assert (await other.status())["status"] == "unavailable"


def test_failed_build_leaves_nothing(store):
    class Broken(FakeEmbeddings):
        def embed_documents(self, texts):
            return [[1.0] + [0.0] * 127 for _ in texts]

    with pytest.raises(RuntimeError, match="self-retrieval"):
        build(KNOWLEDGE, store, Broken(), version="v1")
    assert store.versions() == [] and store.active_version() is None
