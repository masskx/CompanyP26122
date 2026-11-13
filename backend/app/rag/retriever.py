"""Query-time retrieval used by the agent's knowledge tool."""

import asyncio
import dataclasses
import json

from app.rag.embeddings import GLMEmbeddings
from app.rag.rerank import Reranker
from app.rag.store import Hit, KnowledgeStore, KnowledgeUnavailable


class Retriever:
    def __init__(self, store: KnowledgeStore, embeddings: GLMEmbeddings, *, top_k: int = 5,
                 min_score: float = 0.3, pinned_version: str | None = None,
                 classifications: tuple[str, ...] = ("public", "internal"),
                 reranker: Reranker | None = None, candidates: int = 20) -> None:
        self.store = store
        self.reranker = reranker
        self._candidates = candidates
        self.embeddings = embeddings
        self._top_k = top_k
        self._min_score = min_score
        self._pinned = pinned_version
        self._filter = (f"classification in {json.dumps(list(classifications))} "
                        'and access_scope == "all"')  # role-based scopes once users have roles

    async def search(self, query: str, top_k: int | None = None, version: str | None = None
                     ) -> tuple[str, list[Hit]]:
        """Returns (version used, hits above KB_MIN_SCORE). Raises KnowledgeUnavailable."""
        version, _ = await asyncio.to_thread(self.store.resolve, self.embeddings.spec,
                                             version or self._pinned)
        try:
            vector = await self.embeddings.aembed_query(query)
            k = min(top_k or self._top_k, 20)
            hits = await asyncio.to_thread(self.store.search, version, vector,
                                           max(k, self._candidates) if self.reranker else k,
                                           self._filter)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(f"knowledge search failed: {exc}") from exc
        hits = [h for h in hits if h.score >= self._min_score]
        if self.reranker and hits:
            order = await self.reranker.rank(query, [h.text for h in hits])
            if order is not None:  # rerank score replaces the vector score
                hits = [dataclasses.replace(hits[i], score=score) for i, score in order]
        return version, hits[:k]

    async def status(self) -> dict:
        try:
            version, meta = await asyncio.to_thread(self.store.resolve, self.embeddings.spec,
                                                    self._pinned)
        except KnowledgeUnavailable as exc:
            return {"status": "unavailable", "detail": str(exc)}
        return {"status": "ok", "version": version, "chunks": meta.get("chunks"),
                "documents": meta.get("documents"), "embedding": meta.get("embedding"),
                "rerank": self.reranker.model or self.reranker.base_url if self.reranker
                else None}
