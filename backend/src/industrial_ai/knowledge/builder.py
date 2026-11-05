"""Offline knowledge build: manifest -> verify -> parse -> chunk -> embed -> Milvus -> verify
-> (optionally) activate. Runs outside the API (see knowledge/cli.py).

If anything fails before activation, the new collection is dropped and the active alias is
untouched, so queries keep using the previous release (FR-KB-04).
"""

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from industrial_ai.integrations.embedding_client import EmbeddingSpec
from industrial_ai.knowledge.chunker import CHUNKER_VERSION, Chunk, chunk_document
from industrial_ai.knowledge.loaders import load_document
from industrial_ai.knowledge.manifest import load_manifest, verify_documents
from industrial_ai.knowledge.milvus_store import MilvusKnowledgeStore

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class Embedder(Protocol):
    @property
    def spec(self) -> EmbeddingSpec: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class BuildError(RuntimeError):
    pass


@dataclass
class BuildReport:
    version: str
    collection: str
    documents: int
    chunks: int
    embedding: dict
    activated: bool
    previous_version: str | None
    manifest_sha256: str
    built_at: str

    def to_dict(self) -> dict:
        return asdict(self)


async def build_knowledge_base(
    manifest_path: Path,
    store: MilvusKnowledgeStore,
    embedder: Embedder,
    *,
    chunk_chars: int,
    chunk_overlap: int,
    activate: bool = True,
    replace: bool = False,
    embed_group: int = 256,
) -> BuildReport:
    manifest, manifest_sha = load_manifest(manifest_path)
    files = verify_documents(manifest, manifest_path.parent)
    logger.info("manifest verified", extra={"fields": {
        "version": manifest.version, "documents": len(files)}})

    chunks: list[Chunk] = []
    for doc in manifest.documents:
        sections = load_document(files[doc.doc_id])
        doc_chunks = chunk_document(doc, sections, max_chars=chunk_chars, overlap=chunk_overlap)
        if not doc_chunks:
            raise BuildError(f"{doc.doc_id}: no text extracted")
        chunks.extend(doc_chunks)

    vectors: list[list[float]] = []
    for i in range(0, len(chunks), embed_group):
        group = chunks[i:i + embed_group]
        vectors.extend(await embedder.embed([c.embedding_text() for c in group]))
        logger.info("embedding progress", extra={"fields": {
            "done": len(vectors), "total": len(chunks)}})

    spec = embedder.spec
    built_at = datetime.now(UTC).isoformat()
    meta = {
        "schema": SCHEMA_VERSION, "kb_version": manifest.version, "title": manifest.title,
        "embedding": spec.to_dict(), "metric": "COSINE",
        "chunker": {"version": CHUNKER_VERSION, "chunk_chars": chunk_chars,
                    "overlap": chunk_overlap},
        "manifest_sha256": manifest_sha, "documents": len(manifest.documents),
        "chunks": len(chunks), "built_at": built_at,
    }

    if await asyncio.to_thread(store.exists, manifest.version):
        if not replace:
            raise BuildError(f"version {manifest.version!r} already exists (use --replace)")
        await asyncio.to_thread(store.drop, manifest.version)  # refuses the active version

    collection = await asyncio.to_thread(store.create_version, manifest.version,
                                         spec.dimensions, meta)
    try:
        await asyncio.to_thread(store.insert, manifest.version, chunks, vectors)
        stored = await asyncio.to_thread(store.finalize, manifest.version)
        if stored != len(chunks):
            raise BuildError(f"stored {stored} chunks, expected {len(chunks)}")
        await _self_retrieval_check(store, manifest.version, chunks, vectors)
        previous = await asyncio.to_thread(store.activate, manifest.version) if activate \
            else await asyncio.to_thread(store.active_version)
    except BaseException:
        logger.error("knowledge build failed; dropping partial collection",
                     extra={"fields": {"version": manifest.version}})
        await asyncio.to_thread(store.client.drop_collection, collection)
        raise

    return BuildReport(
        version=manifest.version, collection=collection, documents=len(manifest.documents),
        chunks=len(chunks), embedding=spec.to_dict(), activated=activate,
        previous_version=previous, manifest_sha256=manifest_sha, built_at=built_at,
    )


async def _self_retrieval_check(store: MilvusKnowledgeStore, version: str,
                                chunks: list[Chunk], vectors: list[list[float]],
                                samples: int = 5) -> None:
    """Each sampled chunk must find itself among the top 3 hits (index sanity check)."""
    step = max(1, len(chunks) // samples)
    for i in range(0, len(chunks), step)[:samples]:
        hits = await asyncio.to_thread(store.search, version, vectors[i], top_k=3,
                                       filter_expr="")
        if chunks[i].chunk_id not in {h["id"] for h in hits}:
            raise BuildError(f"self-retrieval check failed for {chunks[i].chunk_id}")
