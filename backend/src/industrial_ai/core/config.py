"""Application settings loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: `KEY=` in .env means "use the default", not an empty string.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      env_ignore_empty=True, populate_by_name=True)

    app_env: str = "development"
    log_level: str = "INFO"

    # Service authentication: the MES main backend is the only trusted caller.
    compute_service_token: SecretStr | None = None

    # ---- End-user identity (see core/identity.py) ----
    # dev:             X-User-Id header or DEV_DEFAULT_USER; refused when APP_ENV=production
    # trusted_gateway: X-User-Id / X-User-Name / X-User-Roles set by the MES main backend, trusted
    #                  only because the request carries the service token (current design)
    # jwt:             signed user assertion issued by the client's identity provider
    user_identity_mode: Literal["dev", "trusted_gateway", "jwt"] = "trusted_gateway"
    dev_default_user: str = "dev-user"
    trusted_gateway_issuer: str = "mes-main-backend"
    user_jwt_header: str = "X-User-Assertion"
    user_jwt_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    user_jwt_key: SecretStr | None = None  # HMAC secret or PEM public key
    user_jwt_key_file: Path | None = None  # alternative to USER_JWT_KEY
    user_jwt_issuer: str | None = None
    user_jwt_audience: str | None = None
    user_jwt_leeway_seconds: int = Field(default=30, ge=0)
    user_jwt_name_claim: str = "name"
    user_jwt_roles_claim: str = "roles"

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
    # Redirection only helps multi-node clusters; off avoids clients being redirected to a
    # container-internal hostname they cannot resolve.
    iotdb_enable_redirection: bool = False

    # ---- Collector: source -> parser -> validator -> IoTDB ----
    # simulator: synthetic radiation dose-rate data (development/testing)
    # mqtt:      subscribe to the broker (real devices, or `collector publish-sim`)
    collector_source: Literal["simulator", "mqtt"] = "simulator"
    # Wire-format parser; add the real format to collector.parser.PARSERS when known.
    collector_parser: str = "provisional_json"
    # JSON mapping file for COLLECTOR_PARSER=mapping (see examples/mqtt/mapping.example.json).
    collector_parser_config: Path | None = None
    collector_topic_prefix: str = "sim/radiation"
    collector_batch_size: int = Field(default=500, ge=1)
    collector_flush_seconds: float = Field(default=1.0, gt=0)
    collector_queue_size: int = Field(default=10_000, ge=1)
    collector_max_buffer: int = Field(default=100_000, ge=1)
    collector_max_future_skew_seconds: float = Field(default=300, ge=0)
    collector_max_age_hours: float | None = Field(default=None, gt=0)
    collector_reject_unknown_equipment: bool = True
    # Run the collector inside the API process (local development, e.g. with the memory
    # backend). In deployment the collector runs as its own process.
    api_embedded_collector: bool = False

    simulator_scenario_path: Path | None = None
    simulator_backfill_hours: float = Field(default=48, ge=0)
    simulator_realtime: bool = True
    simulator_seed: int | None = 42

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topics: list[str] = Field(default_factory=lambda: ["sim/radiation/#"])
    mqtt_qos: int = Field(default=1, ge=0, le=2)
    mqtt_client_id: str | None = None
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None
    mqtt_keepalive: int = Field(default=60, ge=1)
    mqtt_tls: bool = False
    mqtt_ca_file: Path | None = None
    mqtt_persistent_session: bool = False

    # Analysis results stay in memory for now.
    analysis_store_backend: Literal["memory"] = "memory"
    # Users, chat sessions, messages, citations, feedback.
    session_store_backend: Literal["memory", "opengauss"] = "memory"
    # libpq DSN, e.g. postgresql://app:***@127.0.0.1:5432/industrial_ai
    opengauss_dsn: SecretStr | None = None
    opengauss_pool_min: int = Field(default=1, ge=0)
    opengauss_pool_max: int = Field(default=10, ge=1)

    # Deterministic rule parameters.
    health_window_hours: int = Field(default=24, ge=1)
    min_data_completeness: float = Field(default=0.8, ge=0.0, le=1.0)
    consecutive_breach_count: int = Field(default=3, ge=1)
    analysis_max_range_days: int = Field(default=90, ge=1)
    max_equipment_per_analysis: int = Field(default=50, ge=1)
    analysis_timeout_seconds: float = Field(default=300, gt=0)

    # Streaming.
    sse_heartbeat_seconds: float = Field(default=15, gt=0)

    # Assistant.
    assistant_max_message_chars: int = Field(default=4000, ge=1)
    assistant_history_messages: int = Field(default=20, ge=0)  # prior turns sent to the model
    assistant_max_tool_steps: int = Field(default=4, ge=1, le=10)
    assistant_tool_result_chars: int = Field(default=8000, ge=500)
    assistant_max_answer_tokens: int = Field(default=2048, ge=64)

    # ---- Embedding (GLM). Index build and query MUST use the same model + dimensions;
    # the retriever refuses to search a collection built with a different configuration.
    # none:  knowledge search disabled
    # zhipu: Zhipu BigModel public API (development only; refused in production)
    # local: privately deployed endpoint with the same request/response shape
    embedding_provider: Literal["none", "zhipu", "local"] = "none"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    # Provider-specific key, used only when EMBEDDING_PROVIDER=zhipu and EMBEDDING_API_KEY is unset.
    glm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GLM_API_KEY", "ZHIPU_API_KEY"))
    embedding_model: str = "embedding-3"
    embedding_dimensions: int | None = Field(default=1024, ge=1)  # embedding-3: 256..2048
    embedding_batch_size: int = Field(default=16, ge=1, le=64)
    embedding_max_batch_chars: int = Field(default=2800, ge=100)
    embedding_timeout_seconds: float = Field(default=30, gt=0)
    embedding_max_retries: int = Field(default=2, ge=0, le=5)

    # ---- Milvus knowledge base ----
    milvus_uri: str | None = None  # http://127.0.0.1:19530 (or a local file for Milvus Lite)
    milvus_token: SecretStr | None = None  # "user:password" or API token
    milvus_db_name: str = "default"
    kb_alias: str = Field(default="nuclear_kb", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    kb_index_type: str = "HNSW"
    kb_top_k: int = Field(default=5, ge=1, le=20)
    kb_min_score: float = Field(default=0.3, ge=-1, le=1)  # cosine similarity floor
    kb_allowed_classifications: list[str] = Field(default_factory=lambda: ["public", "internal"])
    kb_chunk_chars: int = Field(default=800, ge=100)
    kb_chunk_overlap: int = Field(default=120, ge=0)

    # LLM. Both providers speak the OpenAI-compatible chat-completions API, so switching
    # from the DeepSeek public API (development/testing) to local vLLM is configuration only.
    #   none     - no model; rule-based text only
    #   deepseek - public internet API; never allowed in production, synthetic data only
    #   vllm     - locally deployed model (production target)
    llm_provider: Literal["none", "deepseek", "vllm"] = "none"
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    # Provider-specific key, used only when LLM_PROVIDER=deepseek and LLM_API_KEY is unset.
    deepseek_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DEEPSEEK_API"))
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

    # Pin queries to one knowledge version instead of following the alias (optional).
    knowledge_index_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]+$")

    @model_validator(mode="after")
    def _check_llm(self) -> "Settings":
        if self.llm_provider == "deepseek":
            if self.app_env == "production":
                raise ValueError(
                    "LLM_PROVIDER=deepseek sends data to a public API and is not allowed when "
                    "APP_ENV=production; use LLM_PROVIDER=vllm"
                )
            if self.resolved_llm_api_key is None:
                raise ValueError(
                    "LLM_API_KEY (or DEEPSEEK_API_KEY / DEEPSEEK_API) is required for "
                    "LLM_PROVIDER=deepseek")
        if self.llm_provider == "vllm" and not (self.llm_base_url and self.llm_model):
            raise ValueError("LLM_BASE_URL and LLM_MODEL are required for LLM_PROVIDER=vllm")
        return self

    @model_validator(mode="after")
    def _check_identity_and_stores(self) -> "Settings":
        production = self.app_env == "production"
        if self.user_identity_mode == "dev" and production:
            raise ValueError("USER_IDENTITY_MODE=dev is not allowed when APP_ENV=production")
        if self.user_identity_mode == "jwt":
            if not (self.user_jwt_key or self.user_jwt_key_file):
                raise ValueError("USER_JWT_KEY or USER_JWT_KEY_FILE is required for jwt mode")
            if not (self.user_jwt_issuer and self.user_jwt_audience):
                raise ValueError("USER_JWT_ISSUER and USER_JWT_AUDIENCE are required for jwt mode")
        if self.session_store_backend == "opengauss" and self.opengauss_dsn is None:
            raise ValueError("OPENGAUSS_DSN is required for SESSION_STORE_BACKEND=opengauss")
        if self.embedding_provider == "zhipu":
            if production:
                raise ValueError(
                    "EMBEDDING_PROVIDER=zhipu sends text to a public API and is not allowed when "
                    "APP_ENV=production; use a private deployment (EMBEDDING_PROVIDER=local)"
                )
            if self.resolved_embedding_api_key is None:
                raise ValueError(
                    "EMBEDDING_API_KEY (or GLM_API_KEY / ZHIPU_API_KEY) is required for "
                    "EMBEDDING_PROVIDER=zhipu")
        if self.embedding_provider == "local" and not self.embedding_base_url:
            raise ValueError("EMBEDDING_BASE_URL is required for EMBEDDING_PROVIDER=local")
        if self.kb_chunk_overlap >= self.kb_chunk_chars:
            raise ValueError("KB_CHUNK_OVERLAP must be smaller than KB_CHUNK_CHARS")
        return self

    @property
    def resolved_llm_api_key(self) -> str | None:
        for key in (self.llm_api_key,
                    self.deepseek_api_key if self.llm_provider == "deepseek" else None):
            if key is not None and key.get_secret_value():
                return key.get_secret_value()
        return None

    @property
    def resolved_embedding_api_key(self) -> str | None:
        for key in (self.embedding_api_key,
                    self.glm_api_key if self.embedding_provider == "zhipu" else None):
            if key is not None and key.get_secret_value():
                return key.get_secret_value()
        return None

    @property
    def resolved_embedding_base_url(self) -> str | None:
        if self.embedding_provider == "zhipu":
            return self.embedding_base_url or "https://open.bigmodel.cn/api/paas/v4"
        return self.embedding_base_url if self.embedding_provider == "local" else None

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
