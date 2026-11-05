"""Chat model factory. DeepSeek (development) and vLLM (production) both speak the OpenAI
API, so one LangChain `ChatOpenAI` covers both; switching is configuration only."""

from langchain_openai import ChatOpenAI

from app.config import Settings


def build_llm(s: Settings) -> ChatOpenAI | None:
    if s.llm_provider == "none":
        return None
    extra = {}
    if s.llm_provider == "vllm":
        # Qwen3: thinking off by default (reasoning is never shown to users).
        extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": s.llm_enable_thinking}}
    return ChatOpenAI(
        model=s.llm_model_name, base_url=s.llm_url,
        api_key=s.llm_key or "EMPTY",  # vLLM without --api-key accepts any value
        temperature=s.llm_temperature, max_tokens=s.llm_max_tokens,
        timeout=s.llm_timeout_seconds, max_retries=2, streaming=True, stream_usage=True,
        **extra,
    )


def model_label(s: Settings) -> str | None:
    return f"{s.llm_provider}:{s.llm_model_name}" if s.llm_provider != "none" else None
