"""GLM embeddings as a LangChain `Embeddings`.

Zhipu BigModel `POST {base}/embeddings` (or a private deployment with the same API):
    request  {"model": "embedding-3", "input": [...], "dimensions": 1024}
    response {"data": [{"index": i, "embedding": [...]}]}
Limits for embedding-3: <= 64 inputs and 3072 tokens per request; dimensions 256/512/1024/2048.
"""

import asyncio
import time

import httpx
from langchain_core.embeddings import Embeddings

from app.config import Settings


class EmbeddingError(RuntimeError):
    pass


class GLMEmbeddings(Embeddings):
    def __init__(self, *, base_url: str, model: str, dimensions: int, api_key: str | None = None,
                 batch_size: int = 16, max_batch_chars: int = 2800, timeout: float = 30,
                 retries: int = 2, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._batch_size = batch_size
        self._max_chars = max_batch_chars
        self._timeout = timeout
        self._retries = retries
        self._transport = transport

    @classmethod
    def from_settings(cls, s: Settings) -> "GLMEmbeddings | None":
        if s.embedding_provider == "none":
            return None
        return cls(base_url=s.embedding_url or "", model=s.embedding_model,
                   dimensions=s.embedding_dimensions, api_key=s.embedding_key,
                   batch_size=s.embedding_batch_size)

    @property
    def spec(self) -> dict:
        """What index build and query must agree on."""
        return {"model": self.model, "dimensions": self.dimensions}

    def _batches(self, texts: list[str]) -> list[list[str]]:
        out, cur, size = [], [], 0
        for t in texts:
            if cur and (len(cur) >= self._batch_size or size + len(t) > self._max_chars):
                out.append(cur)
                cur, size = [], 0
            cur.append(t)
            size += len(t)
        return out + ([cur] if cur else [])

    def _body(self, batch: list[str]) -> dict:
        body: dict = {"model": self.model, "input": batch}
        if self.model != "embedding-2":  # embedding-2 has a fixed size
            body["dimensions"] = self.dimensions
        return body

    def _parse(self, resp: httpx.Response, n: int) -> list[list[float]]:
        if resp.status_code != 200:
            raise EmbeddingError(f"embedding service returned HTTP {resp.status_code}: "
                                 f"{resp.text[:200]}")
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        vectors = [[float(x) for x in d["embedding"]] for d in data]
        if len(vectors) != n or any(len(v) != self.dimensions for v in vectors):
            raise EmbeddingError(f"expected {n} vectors of {self.dimensions} dimensions")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        with httpx.Client(timeout=self._timeout, headers=self._headers,
                          transport=self._transport) as client:
            for batch in self._batches(texts):
                for attempt in range(self._retries + 1):
                    try:
                        resp = client.post(f"{self.base_url}/embeddings", json=self._body(batch))
                        if resp.status_code in (429, 500, 502, 503, 504) and \
                                attempt < self._retries:
                            raise httpx.TransportError(f"HTTP {resp.status_code}")
                        out.extend(self._parse(resp, len(batch)))
                        break
                    except httpx.TransportError:
                        if attempt == self._retries:
                            raise
                        time.sleep(2 ** attempt)
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_documents, texts)

    async def aembed_query(self, text: str) -> list[float]:
        return (await self.aembed_documents([text]))[0]
