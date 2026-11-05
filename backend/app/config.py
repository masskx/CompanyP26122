"""All settings, read from environment variables and `.env` (see .env.example)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      env_ignore_empty=True, populate_by_name=True)

    # ---- general -----------------------------------------------------------------------
    app_env: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    # Optional shared key for the whole API (X-API-Key). Empty = open (no auth for now).
    api_key: SecretStr | None = None
    cors_origins: list[str] = Field(default_factory=list)

    # ---- devices -----------------------------------------------------------------------
    devices_file: Path = BASE_DIR / "data" / "devices.json"

    # ---- Apache IoTDB ------------------------------------------------------------------
    timeseries_backend: Literal["iotdb", "memory"] = "iotdb"  # memory: demos/tests only
    iotdb_host: str = "127.0.0.1"
    iotdb_port: int = 6667
    iotdb_user: str = "root"
    iotdb_password: SecretStr = SecretStr("root")
    iotdb_root: str = Field(default="root.mes", pattern=r"^root(\.[A-Za-z0-9_]+)*$")
    iotdb_pool_size: int = 4

    # ---- openGauss ---------------------------------------------------------------------
    # libpq DSN, e.g. "host=127.0.0.1 port=15432 dbname=industrial_ai user=app_ai password=..."
    # Empty = conversations kept in memory (lost on restart).
    opengauss_dsn: SecretStr | None = None

    # ---- LLM (OpenAI-compatible: DeepSeek for development, vLLM in production) ---------
    llm_provider: Literal["none", "deepseek", "vllm"] = "none"
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DEEPSEEK_API"))
    llm_temperature: float = 0.1
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 60
    llm_enable_thinking: bool = False  # Qwen3 on vLLM; reasoning is never shown to users

    # ---- Agent -------------------------------------------------------------------------
    agent_max_tool_rounds: int = Field(default=4, ge=1, le=10)
    agent_history_messages: int = Field(default=20, ge=0)
    agent_max_input_chars: int = Field(default=4000, ge=1)

    # ---- GLM embedding + Milvus knowledge base -----------------------------------------
    embedding_provider: Literal["none", "zhipu", "local"] = "none"
    embedding_base_url: str | None = None
    embedding_model: str = "embedding-3"
    embedding_dimensions: int = 1024  # embedding-3: 256 | 512 | 1024 | 2048
    embedding_api_key: SecretStr | None = None
    glm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GLM_API_KEY", "ZHIPU_API_KEY"))
    embedding_batch_size: int = Field(default=16, ge=1, le=64)

    milvus_uri: str | None = None  # http://127.0.0.1:19530 ; empty = knowledge search off
    milvus_token: SecretStr | None = None
    kb_name: str = Field(default="nuclear_kb", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    kb_top_k: int = Field(default=5, ge=1, le=20)
    kb_min_score: float = 0.3
    kb_chunk_size: int = 800
    kb_chunk_overlap: int = 120
    kb_classifications: list[str] = Field(default_factory=lambda: ["public", "internal"])
    kb_version: str | None = None  # pin queries to one version instead of the active one

    # ---- Ingestion (simulator / MQTT -> IoTDB) -------------------------------------------
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topics: list[str] = Field(default_factory=lambda: ["sim/radiation/#"])
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None
    mqtt_tls: bool = False
    mqtt_client_id: str | None = None
    mqtt_qos: int = 1
    # Message format: "simulator" (built-in), "mapping" (JSON file), or "module:Class".
    ingest_parser: str = "simulator"
    ingest_mapping_file: Path | None = None
    ingest_topic_prefix: str = "sim/radiation"
    ingest_batch_size: int = 500
    ingest_flush_seconds: float = 1.0
    ingest_reject_unknown_devices: bool = True
    # Generate simulated data inside the API process (database-free demos).
    demo_simulation: bool = False
    simulator_backfill_hours: float = 48
    simulator_seed: int | None = 42

    # ---- Rules ---------------------------------------------------------------------------
    rule_min_completeness: float = 0.8
    rule_consecutive_breaches: int = 3

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        prod = self.app_env == "production"
        if self.llm_provider == "deepseek":
            if prod:
                raise ValueError("LLM_PROVIDER=deepseek (public API) is not allowed in production")
            if not self.llm_key:
                raise ValueError("DeepSeek needs LLM_API_KEY or DEEPSEEK_API / DEEPSEEK_API_KEY")
        if self.llm_provider == "vllm" and not (self.llm_base_url and self.llm_model):
            raise ValueError("LLM_PROVIDER=vllm needs LLM_BASE_URL and LLM_MODEL")
        if self.embedding_provider == "zhipu":
            if prod:
                raise ValueError("EMBEDDING_PROVIDER=zhipu (public API) is not allowed in "
                                 "production; use a private deployment (local)")
            if not self.embedding_key:
                raise ValueError("Zhipu needs EMBEDDING_API_KEY or GLM_API_KEY / ZHIPU_API_KEY")
        if self.embedding_provider == "local" and not self.embedding_base_url:
            raise ValueError("EMBEDDING_PROVIDER=local needs EMBEDDING_BASE_URL")
        return self

    # Provider-specific keys are only used with their own provider.
    @property
    def llm_key(self) -> str | None:
        for key in (self.llm_api_key, self.deepseek_api_key if self.llm_provider == "deepseek"
                    else None):
            if key and key.get_secret_value():
                return key.get_secret_value()
        return None

    @property
    def embedding_key(self) -> str | None:
        for key in (self.embedding_api_key, self.glm_api_key if self.embedding_provider == "zhipu"
                    else None):
            if key and key.get_secret_value():
                return key.get_secret_value()
        return None

    @property
    def llm_url(self) -> str | None:
        return self.llm_base_url or ("https://api.deepseek.com" if self.llm_provider == "deepseek"
                                     else None)

    @property
    def llm_model_name(self) -> str | None:
        return self.llm_model or ("deepseek-chat" if self.llm_provider == "deepseek" else None)

    @property
    def embedding_url(self) -> str | None:
        if self.embedding_provider == "zhipu":
            return self.embedding_base_url or "https://open.bigmodel.cn/api/paas/v4"
        return self.embedding_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
