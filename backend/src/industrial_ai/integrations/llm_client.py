"""OpenAI-compatible chat-completions client.

One client serves both the DeepSeek public API (development/testing) and a local vLLM
server (production), because both implement `POST {base_url}/chat/completions`. Switching
is configuration only (LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY).

Prompt and completion text are never logged; only latency, token usage and error types.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from industrial_ai.core.config import Settings

logger = logging.getLogger(__name__)

Provider = Literal["deepseek", "vllm"]
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class LLMConfig:
    provider: Provider
    base_url: str
    model: str
    api_key: str | None = None
    precision: str | None = None
    revision: str | None = None
    timeout_seconds: float = 60
    max_retries: int = 1
    max_concurrency: int = 4
    temperature: float = 0.1
    max_tokens: int = 1024
    enable_thinking: bool = False
    retry_backoff_seconds: float = 0.5

    @classmethod
    def from_settings(cls, s: Settings) -> "LLMConfig | None":
        if s.llm_provider == "none":
            return None
        base_url, model = s.resolved_llm_base_url, s.resolved_llm_model
        assert base_url and model  # guaranteed by Settings validation
        return cls(
            provider=s.llm_provider, base_url=base_url, model=model,
            api_key=s.llm_api_key.get_secret_value() if s.llm_api_key else None,
            precision=s.llm_model_precision, revision=s.llm_model_revision,
            timeout_seconds=s.llm_timeout_seconds, max_retries=s.llm_max_retries,
            max_concurrency=s.llm_max_concurrency, temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens, enable_thinking=s.llm_enable_thinking,
        )


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as produced by the model


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass
class StreamResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def _partial_suffix(text: str, tag: str) -> int:
    """Length of the longest suffix of `text` that is a proper prefix of `tag`."""
    for k in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:k]):
            return k
    return 0


class ThinkFilter:
    """Removes <think>…</think> from a token stream, even when tags are split across chunks."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while True:
            if self._inside:
                end = self._buf.find(self.CLOSE)
                if end == -1:
                    keep = _partial_suffix(self._buf, self.CLOSE)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                self._buf = self._buf[end + len(self.CLOSE):]
                self._inside = False
            else:
                start = self._buf.find(self.OPEN)
                if start == -1:
                    safe = len(self._buf) - _partial_suffix(self._buf, self.OPEN)
                    out.append(self._buf[:safe])
                    self._buf = self._buf[safe:]
                    break
                out.append(self._buf[:start])
                self._buf = self._buf[start + len(self.OPEN):]
                self._inside = True
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self._inside else self._buf
        self._buf = ""
        return rest


def clean_content(text: str) -> str:
    """Drop any <think> block and a surrounding ```json fence from model output."""
    text = _THINK_RE.sub("", text).strip()
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text


class LLMClient:
    def __init__(self, config: LLMConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._http = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"), headers=headers,
            timeout=httpx.Timeout(config.timeout_seconds), transport=transport,
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    def _payload(self, messages: list[dict[str, str]], json_mode: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.config.provider == "vllm":
            # Qwen3 chat template switch; DeepSeek does not accept this field.
            payload["chat_template_kwargs"] = {"enable_thinking": self.config.enable_thinking}
        return payload

    async def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> ChatResult:
        payload = self._payload(messages, json_mode)
        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                async with self._semaphore:
                    return await self._once(payload)
            except LLMError as exc:
                if not exc.retryable or attempt == attempts:
                    raise
                await asyncio.sleep(self.config.retry_backoff_seconds * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    async def _once(self, payload: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        try:
            resp = await self._http.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError("model request timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise LLMError("model service unreachable", retryable=True) from exc

        if resp.status_code != 200:
            raise LLMError(f"model service returned HTTP {resp.status_code}",
                           retryable=resp.status_code in _RETRYABLE_STATUS)
        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("malformed model response", retryable=True) from exc

        usage = body.get("usage") or {}
        result = ChatResult(
            content=clean_content(content), model=body.get("model") or self.config.model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        logger.info("llm call", extra={"fields": {
            "provider": self.config.provider, "model": result.model,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens}})
        return result

    async def stream_chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[TextDelta | StreamResult]:
        """Stream a completion. Yields TextDelta chunks, then exactly one StreamResult.

        Transient failures are retried only before the first chunk has been yielded.
        """
        payload = self._payload(messages, json_mode=False)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            started = False
            try:
                async with self._semaphore:
                    async for item in self._stream_once(payload):
                        started = True
                        yield item
                return
            except LLMError as exc:
                if started or not exc.retryable or attempt == attempts:
                    raise
                await asyncio.sleep(self.config.retry_backoff_seconds * 2 ** (attempt - 1))

    async def _stream_once(self, payload: dict[str, Any]
                           ) -> AsyncIterator[TextDelta | StreamResult]:
        t0 = time.perf_counter()
        think = ThinkFilter()
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        result = StreamResult(content="")
        try:
            async with self._http.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise LLMError(f"model service returned HTTP {resp.status_code}",
                                   retryable=resp.status_code in _RETRYABLE_STATUS)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise LLMError("malformed stream chunk", retryable=False) from exc
                    usage = chunk.get("usage") or {}
                    if usage:
                        result.prompt_tokens = usage.get("prompt_tokens")
                        result.completion_tokens = usage.get("completion_tokens")
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            visible = think.feed(delta["content"])
                            if visible:
                                text.append(visible)
                                yield TextDelta(visible)
                        for tc in delta.get("tool_calls") or []:
                            entry = calls.setdefault(int(tc.get("index", 0)),
                                                     {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                entry["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name") and not entry["name"]:
                                entry["name"] = fn["name"]
                            entry["arguments"] += fn.get("arguments") or ""
                        result.finish_reason = choice.get("finish_reason") or result.finish_reason
        except httpx.TimeoutException as exc:
            raise LLMError("model request timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise LLMError("model service unreachable", retryable=True) from exc

        tail = think.flush()
        if tail:
            text.append(tail)
            yield TextDelta(tail)
        result.content = "".join(text)
        result.tool_calls = [ToolCall(id=c["id"] or f"call_{i}", name=c["name"],
                                      arguments=c["arguments"])
                             for i, c in sorted(calls.items()) if c["name"]]
        logger.info("llm stream", extra={"fields": {
            "provider": self.config.provider, "model": self.config.model,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
            "tool_calls": [c.name for c in result.tool_calls]}})
        yield result

    async def aclose(self) -> None:
        await self._http.aclose()
