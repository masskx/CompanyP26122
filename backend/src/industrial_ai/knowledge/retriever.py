"""Online knowledge retrieval used by the agent's search tool.

* Resolves the active release once per query (or uses KNOWLEDGE_INDEX_VERSION if pinned),
  so a single answer only ever sees one release.
* Refuses to search a collection whose recorded embedding model/dimensions differ from the
  query embedder (vectors from different models are not comparable).
* Filters by document classification and access scope (user roles).
* Drops hits below KB_MIN_SCORE; an empty result means "no basis found".
"""

import asyncio
import json
import re
from dataclasses import dataclass

from industrial_ai.knowledge.builder import Embedder
from industrial_ai.knowledge.milvus_store import MilvusKnowledgeStore

_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class KnowledgeBaseUnavailable(Exception):
    pass


class KnowledgeBaseIncompatible(KnowledgeBaseUnavailable):
    pass


@dataclass(frozen=True)
class KnowledgeHit:
    chunk_id: str
    doc_id: str
    title: str
    source: str | None
    section: str | None
    page: int | None
    doc_version: str | None
    text: str
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    kb_version: str
    hits: list[KnowledgeHit]


class KnowledgeRetriever:
    def __init__(self, store: MilvusKnowledgeStore, embedder: Embedder, *,
                 pinned_version: str | None, top_k: int, min_score: float,
                 allowed_classifications: list[str]) -> None:
        self._store = store
        self._embedder = embedder
        self._pinned = pinned_version
        self._top_k = top_k
        self._min_score = min_score
        self._classifications = [c for c in allowed_classifications if _TOKEN.fullmatch(c)]
        self._checked: dict[str, dict] = {}

    async def _resolve(self, version: str | None = None) -> tuple[str, dict]:
        try:
            version = version or self._pinned or await asyncio.to_thread(
                self._store.active_version)
            if version is None:
                raise KnowledgeBaseUnavailable("no active knowledge version")
            meta = self._checked.get(version)
            if meta is None:
                meta = await asyncio.to_thread(self._store.metadata, version)
                expected = self._embedder.spec.to_dict()
                if meta.get("embedding") != expected:
                    raise KnowledgeBaseIncompatible(
                        f"knowledge version {version} was built with {meta.get('embedding')}, "
                        f"but queries use {expected}")
                self._checked[version] = meta
            return version, meta
        except KnowledgeBaseUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeBaseUnavailable("vector database unavailable") from exc

    def filter_expression(self, roles: tuple[str, ...]) -> str:
        scopes = ["all", *[r for r in roles if _TOKEN.fullmatch(r)]]
        return (f"classification in {json.dumps(self._classifications)} "
                f"and access_scope in {json.dumps(scopes)}")

    async def search(self, query: str, *, top_k: int | None = None,
                     roles: tuple[str, ...] = (), version: str | None = None
                     ) -> RetrievalResult:
        """`version` keeps several searches in one answer on the same release."""
        version, _ = await self._resolve(version)
        try:
            vector = await self._embedder.embed([query])
        except Exception as exc:
            raise KnowledgeBaseUnavailable("embedding service unavailable") from exc
        try:
            rows = await asyncio.to_thread(
                self._store.search, version, vector[0], top_k=min(top_k or self._top_k, 20),
                filter_expr=self.filter_expression(roles))
        except Exception as exc:
            raise KnowledgeBaseUnavailable("vector search failed") from exc
        hits = [KnowledgeHit(
            chunk_id=r["id"], doc_id=r["doc_id"], title=r["title"], source=r["source"] or None,
            section=r["section"] or None, page=r["page"] if r["page"] >= 0 else None,
            doc_version=r["doc_version"] or None, text=r["text"], score=r["score"],
        ) for r in rows if r["score"] >= self._min_score]
        return RetrievalResult(kb_version=version, hits=hits)

    async def status(self) -> dict:
        try:
            version, meta = await self._resolve()
        except KnowledgeBaseIncompatible as exc:
            return {"status": "incompatible", "detail": str(exc)}
        except KnowledgeBaseUnavailable as exc:
            return {"status": "unavailable", "detail": str(exc)}
        return {"status": "ok", "version": version, "pinned": self._pinned is not None,
                "chunks": meta.get("chunks"), "embedding": meta.get("embedding")}
