"""GLM embedding client (Zhipu BigModel `/embeddings`, or a private deployment with the
same request/response shape).

Request:  {"model": "embedding-3", "input": [..], "dimensions": 1024}
Response: {"data": [{"index": i, "embedding": [...]}], "usage": {...}}
Limits (embedding-3): up to 64 inputs and 3072 tokens per request; dimensions 256/512/
1024/2048. Batches are therefore bounded by both count and total characters.

The same EmbeddingSpec is recorded on every Milvus collection at build time and checked
before every query (see knowledge/retriever.py), so documents and queries always share
one vector space.
"""

import asyncio
import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from industrial_ai.core.config import Settings

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class EmbeddingError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class EmbeddingSpec:
    """What must match between index build and query."""

    model: str
    dimensions: int

    def to_dict(self) -> dict:
        return {"model": self.model, "dimensions": self.dimensions}


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    dimensions: int
    api_key: str | None = None
    batch_size: int = 16
    max_batch_chars: int = 2800
    timeout_seconds: float = 30
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    send_dimensions: bool = True  # embedding-2 has a fixed size and rejects the field

    @property
    def spec(self) -> EmbeddingSpec:
        return EmbeddingSpec(self.model, self.dimensions)

    @classmethod
    def from_settings(cls, s: Settings) -> "EmbeddingConfig | None":
        if s.embedding_provider == "none":
            return None
        base_url = s.resolved_embedding_base_url
        assert base_url
        fixed = s.embedding_model == "embedding-2"
        return cls(
            base_url=base_url, model=s.embedding_model,
            dimensions=1024 if fixed else (s.embedding_dimensions or 2048),
            api_key=s.resolved_embedding_api_key,
            batch_size=s.embedding_batch_size, max_batch_chars=s.embedding_max_batch_chars,
            timeout_seconds=s.embedding_timeout_seconds, max_retries=s.embedding_max_retries,
            send_dimensions=not fixed and s.embedding_dimensions is not None,
        )


def batches(texts: list[str], max_items: int, max_chars: int) -> Iterator[tuple[int, list[str]]]:
    """Yield (start_index, batch) bounded by item count and total characters."""
    start, current, size = 0, [], 0
    for i, text in enumerate(texts):
        if current and (len(current) >= max_items or size + len(text) > max_chars):
            yield start, current
            start, current, size = i, [], 0
        current.append(text)
        size += len(text)
    if current:
        yield start, current


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._http = httpx.AsyncClient(base_url=config.base_url.rstrip("/"), headers=headers,
                                       timeout=httpx.Timeout(config.timeout_seconds),
                                       transport=transport)

    @property
    def spec(self) -> EmbeddingSpec:
        return self.config.spec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if any(not t.strip() for t in texts):
            raise ValueError("cannot embed empty text")
        out: list[list[float]] = []
        for _, batch in batches(texts, self.config.batch_size, self.config.max_batch_chars):
            out.extend(await self._with_retry(batch))
        return out

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def _with_retry(self, batch: list[str]) -> list[list[float]]:
        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._request(batch)
            except EmbeddingError as exc:
                if not exc.retryable or attempt == attempts:
                    raise
                await asyncio.sleep(self.config.retry_backoff_seconds * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    async def _request(self, batch: list[str]) -> list[list[float]]:
        body: dict = {"model": self.config.model, "input": batch}
        if self.config.send_dimensions:
            body["dimensions"] = self.config.dimensions
        try:
            resp = await self._http.post("/embeddings", json=body)
        except httpx.TimeoutException as exc:
            raise EmbeddingError("embedding request timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise EmbeddingError("embedding service unreachable", retryable=True) from exc
        if resp.status_code != 200:
            raise EmbeddingError(f"embedding service returned HTTP {resp.status_code}",
                                 retryable=resp.status_code in _RETRYABLE_STATUS)
        try:
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors = [[float(x) for x in d["embedding"]] for d in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("malformed embedding response", retryable=True) from exc
        if len(vectors) != len(batch):
            raise EmbeddingError("embedding count does not match input", retryable=False)
        for v in vectors:
            if len(v) != self.config.dimensions or not all(math.isfinite(x) for x in v):
                raise EmbeddingError(
                    f"expected {self.config.dimensions}-dim vectors, got {len(v)}",
                    retryable=False)
        return vectors

    async def aclose(self) -> None:
        await self._http.aclose()
