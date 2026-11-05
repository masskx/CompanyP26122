"""Application settings loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    # Service authentication: the MES main backend is the only trusted caller.
    compute_service_token: SecretStr | None = None

    # Equipment master data. Placeholder until the MES equipment API contract is fixed.
    equipment_catalog_path: Path | None = None

    # Time-series storage: Apache IoTDB. "memory" is for tests and local development only.
    timeseries_backend: Literal["memory", "iotdb"] = "iotdb"
    iotdb_host: str = "127.0.0.1"
    iotdb_port: int = 6667
    iotdb_user: str = "root"
    iotdb_password: SecretStr | None = None
    iotdb_root_path: str = "root.mes"
    iotdb_pool_size: int = Field(default=4, ge=1)
    iotdb_wait_timeout_ms: int = Field(default=3000, ge=1)

    # Relational storage for analysis results and assistant sessions is not finalised.
    analysis_store_backend: Literal["memory"] = "memory"
    session_store_backend: Literal["memory"] = "memory"

    # Deterministic rule parameters.
    health_window_hours: int = Field(default=24, ge=1)
    min_data_completeness: float = Field(default=0.8, ge=0.0, le=1.0)
    consecutive_breach_count: int = Field(default=3, ge=1)
    analysis_max_range_days: int = Field(default=90, ge=1)
    max_equipment_per_analysis: int = Field(default=50, ge=1)
    analysis_timeout_seconds: float = Field(default=300, gt=0)

    # Streaming.
    sse_heartbeat_seconds: float = Field(default=15, gt=0)

    # Assistant input limits.
    assistant_max_message_chars: int = Field(default=4000, ge=1)

    # LLM. Both providers speak the OpenAI-compatible chat-completions API, so switching
    # from the DeepSeek public API (development/testing) to local vLLM is configuration only.
    #   none     - no model; rule-based text only
    #   deepseek - public internet API; never allowed in production, synthetic data only
    #   vllm     - locally deployed model (production target)
    llm_provider: Literal["none", "deepseek", "vllm"] = "none"
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_model_precision: str | None = None  # e.g. W8A8, recorded with each result
    llm_model_revision: str | None = None
    llm_timeout_seconds: float = Field(default=60, gt=0)
    llm_max_retries: int = Field(default=1, ge=0, le=5)
    llm_max_concurrency: int = Field(default=4, ge=1)
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1024, ge=1)
    # vLLM only: Qwen3 thinking mode. Off by default; reasoning text is never shown to users.
    llm_enable_thinking: bool = False

    # RAG. Not wired yet; reported by the health endpoints.
    knowledge_index_version: str | None = None

    @model_validator(mode="after")
    def _check_llm(self) -> "Settings":
        if self.llm_provider == "deepseek":
            if self.app_env == "production":
                raise ValueError(
                    "LLM_PROVIDER=deepseek sends data to a public API and is not allowed when "
                    "APP_ENV=production; use LLM_PROVIDER=vllm"
                )
            if self.llm_api_key is None or not self.llm_api_key.get_secret_value():
                raise ValueError("LLM_API_KEY is required for LLM_PROVIDER=deepseek")
        if self.llm_provider == "vllm" and not (self.llm_base_url and self.llm_model):
            raise ValueError("LLM_BASE_URL and LLM_MODEL are required for LLM_PROVIDER=vllm")
        return self

    @property
    def resolved_llm_base_url(self) -> str | None:
        if self.llm_provider == "deepseek":
            return self.llm_base_url or "https://api.deepseek.com"
        return self.llm_base_url if self.llm_provider == "vllm" else None

    @property
    def resolved_llm_model(self) -> str | None:
        if self.llm_provider == "deepseek":
            return self.llm_model or "deepseek-chat"
        return self.llm_model if self.llm_provider == "vllm" else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
