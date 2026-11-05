"""OpenAI-compatible chat-completions client.

One client serves both the DeepSeek public API (development/testing) and a local vLLM
server (production), because both implement `POST {base_url}/chat/completions`. Switching
is configuration only (LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY).

Prompt and completion text are never logged; only latency, token usage and error types.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
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

    async def aclose(self) -> None:
        await self._http.aclose()
