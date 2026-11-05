"""Test doubles: a deterministic embedder and a scripted OpenAI-compatible streaming LLM."""

import hashlib
import json
import math

import httpx

from industrial_ai.integrations.embedding_client import EmbeddingSpec
from industrial_ai.integrations.llm_client import LLMClient, LLMConfig


class FakeEmbedder:
    """Character uni/bigram hashing -> normalised vector. Texts sharing words are close."""

    def __init__(self, dims: int = 128, model: str = "fake-glm") -> None:
        self._spec = EmbeddingSpec(model, dims)
        self.calls: list[list[str]] = []

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self._spec.dimensions
        grams = list(text) + [text[i:i + 2] for i in range(len(text) - 1)]
        for g in grams:
            if g.strip():
                v[int(hashlib.md5(g.encode()).hexdigest(), 16) % len(v)] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def aclose(self) -> None:
        return None


def _sse(chunks: list[dict]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=(body + "data: [DONE]\n\n").encode())


def text_reply(text: str, pieces: int = 3) -> httpx.Response:
    step = max(1, len(text) // pieces)
    parts = [text[i:i + step] for i in range(0, len(text), step)]
    chunks = [{"choices": [{"index": 0, "delta": {"content": p}}]} for p in parts]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    chunks.append({"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 20}})
    return _sse(chunks)


def tool_reply(name: str, args: dict, call_id: str = "call_1") -> httpx.Response:
    raw = json.dumps(args, ensure_ascii=False)
    half = len(raw) // 2
    return _sse([
        {"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": raw[:half]}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": raw[half:]}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 40, "completion_tokens": 10}},
    ])


class ScriptedLLM:
    """MockTransport handler replaying queued responses and recording request bodies."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if not self.responses:
            return httpx.Response(500)
        return self.responses.pop(0)

    def client(self) -> LLMClient:
        config = LLMConfig(provider="deepseek", base_url="https://llm.test", model="test-model",
                           api_key="k", retry_backoff_seconds=0)
        return LLMClient(config, transport=httpx.MockTransport(self))
