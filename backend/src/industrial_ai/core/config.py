"""Application settings loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    # Service authentication: the MES main backend is the only trusted caller.
    compute_service_token: SecretStr | None = None

    # Equipment master data. Placeholder until the MES equipment API contract is fixed.
    equipment_catalog_path: Path | None = None

    # Time-series storage. The product is not finalised, so it is pluggable.
    timeseries_backend: Literal["memory", "iotdb"] = "memory"
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

    # Model / RAG. Not wired yet; reported by the health endpoints.
    vllm_base_url: str | None = None
    vllm_model_name: str | None = None
    knowledge_index_version: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
