"""Optional rerank step (RERANK_BASE_URL, design doc §3.2): vector search returns
KB_RERANK_CANDIDATES chunks, a local rerank model orders them, the best KB_TOP_K are used.

Speaks the common `/rerank` API of vLLM (`vllm serve <bge-reranker> --task score`), Jina and
Cohere: POST {base}/rerank {"model", "query", "documents", "top_n"} ->
{"results": [{"index", "relevance_score"}]}. Text-embeddings-inference's
{"query", "texts"} -> [{"index", "score"}] answer is accepted too. When the service fails,
the vector order is kept (logged), so retrieval never breaks because of rerank.
"""

import logging

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class Reranker:
    def __init__(self, base_url: str, model: str | None, api_key: str | None = None,
                 timeout: float = 15) -> None:
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    @classmethod
    def from_settings(cls, s: Settings) -> "Reranker | None":
        if not s.rerank_base_url:
            return None
        return cls(s.rerank_base_url, s.rerank_model_name,
                   s.rerank_api_key.get_secret_value() if s.rerank_api_key else None)

    async def rank(self, query: str, documents: list[str]) -> list[tuple[int, float]] | None:
        """[(index, score)] best first, or None when the service is unavailable."""
        if not documents:
            return []
        body = {"query": query, "documents": documents, "top_n": len(documents)}
        if self.model:
            body["model"] = self.model
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                r = await client.post(f"{self.base_url}/rerank", json=body)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            logger.warning("rerank unavailable, keeping vector order: %s", exc)
            return None
        items = data.get("results", data) if isinstance(data, dict) else data
        out = [(int(i["index"]), float(i.get("relevance_score", i.get("score", 0.0))))
               for i in items]
        return sorted(out, key=lambda x: -x[1])
