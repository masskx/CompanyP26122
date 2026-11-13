"""All settings, read from environment variables and `.env` (see .env.example)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # Service token the main server sends as "Authorization: Bearer <token>" (design doc
    # COMPUTE_SERVICE_TOKEN). Either this or API_KEY authorises a call; both empty = open.
    compute_service_token: SecretStr | None = None
    cors_origins: list[str] = Field(default_factory=list)

    # ---- stations ----------------------------------------------------------------------
    # Station directory snapshot (thresholds, equipment; simulation profiles for the simulator).
    stations_file: Path = BASE_DIR / "data" / "stations.json"
    # Local time zone used for "today / last month / last year" in reports and assessments.
    analysis_timezone: str = "Asia/Shanghai"

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
    llm_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("LLM_BASE_URL", "VLLM_BASE_URL"))
    llm_model: str | None = Field(
        default=None, validation_alias=AliasChoices("LLM_MODEL", "VLLM_MODEL_NAME"))
    llm_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DEEPSEEK_API"))
    llm_temperature: float = 0.1
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 60
    llm_enable_thinking: bool = False  # Qwen3 on vLLM; reasoning is never shown to users
    # Shown in analysis results (versions.model); the model list is the source of truth.
    llm_model_precision: str = "以模型清单为准"
    llm_model_revision: str = "以模型清单为准"

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
    kb_version: str | None = Field(  # pin queries to one version instead of the active one
        default=None, validation_alias=AliasChoices("KB_VERSION", "KNOWLEDGE_INDEX_VERSION"))
    kb_corpus_path: Path = Field(  # default corpus for scripts/build_knowledge.py
        default=BASE_DIR / "data" / "knowledge",
        validation_alias=AliasChoices("KB_CORPUS_PATH", "KNOWLEDGE_CORPUS_PATH"))
    # Optional local rerank model (vLLM / TEI / Jina-compatible /rerank endpoint).
    rerank_base_url: str | None = None
    rerank_model_name: str | None = None
    rerank_api_key: SecretStr | None = None
    kb_rerank_candidates: int = Field(default=20, ge=1, le=100)

    # ---- Ingestion (simulator / MQTT -> IoTDB) -------------------------------------------
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topics: list[str] = Field(default_factory=lambda: ["maintenance/v1/#"])
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None
    mqtt_tls: bool = False
    mqtt_client_id: str | None = None
    mqtt_qos: int = 1
    # Message format: "telemetry_v1" (main server contract), "mapping" (JSON file) or
    # "module:Class".
    ingest_parser: str = "telemetry_v1"
    ingest_mapping_file: Path | None = None
    ingest_topic_prefix: str = "maintenance/v1"
    ingest_batch_size: int = 500
    ingest_flush_seconds: float = 1.0
    ingest_reject_unknown_devices: bool = True
    # Generate simulated data inside the API process (database-free demos).
    demo_simulation: bool = False
    simulator_backfill_hours: float = 48
    simulator_seed: int | None = 42

    # ---- Predictive maintenance analysis (called by the main server) --------------------
    # Main server read-only history API (GET /internal/v1/maintenance/history). Empty = use
    # the work-order copy the main server pushes to this service (openGauss).
    mes_api_base_url: str | None = None
    mes_service_token: SecretStr | None = None
    analysis_watermark_wait_seconds: float = Field(default=30, ge=0)
    analysis_watermark_poll_ms: int = Field(default=500, ge=50)
    analysis_max_window_days: int = Field(default=31, ge=1)
    analysis_idempotency_ttl_seconds: int = Field(default=3600, ge=0)
    maintenance_rule_set_version: str = "maintenance-rules-2026.11.1"
    maintenance_action_catalog_path: Path = BASE_DIR / "data" / "maintenance" / "actions.json"
    maintenance_narrator_prompt_version: str = "maintenance-narrator-v1"
    maintenance_narrator_model_name: str | None = None  # default: LLM_MODEL
    maintenance_narrator_timeout_seconds: float = Field(default=30, gt=0)
    maintenance_narrator_max_chars: int = Field(default=300, ge=50)
    maintenance_use_llm: bool = True  # False: always the rule template
    # rule parameters
    analysis_consecutive_samples: int = Field(default=3, ge=1)  # samples per threshold breach
    analysis_gap_intervals: float = Field(default=3, gt=1)  # gap > N sampling intervals
    analysis_stuck_minutes: int = Field(default=60, ge=5)   # identical values this long
    analysis_min_completeness: float = Field(default=0.8, ge=0, le=1)  # below = insufficient
    analysis_fault_level: Literal["inspect", "watch"] = "inspect"
    analysis_charge_factor: float = Field(default=1.5, gt=1)  # slow charge = factor x expected
    analysis_baseline_days: int = Field(default=14, ge=1, le=60)  # 前两周 baseline
    analysis_rise_watch_pct: float = Field(default=30, gt=0)   # dose rise vs baseline
    analysis_runtime_drop_watch_pct: float = Field(default=15, gt=0)
    analysis_runtime_drop_inspect_pct: float = Field(default=25, gt=0)
    analysis_history_lookback_days: int = Field(default=90, ge=0)

    # ---- Assistant workflows and reports -----------------------------------------------
    # Messages without an explicit workflow or keyword are classified by the model
    # (one short call); False = such messages go straight to the general agent.
    assistant_intent_model: bool = True
    reports_dir: Path = BASE_DIR / "var" / "reports"   # generated Excel files
    report_max_days: int = Field(default=92, ge=1)
    sse_heartbeat_seconds: float = Field(default=15, gt=0)

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
        try:
            ZoneInfo(self.analysis_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown ANALYSIS_TIMEZONE {self.analysis_timezone!r} (on Windows "
                             "the time-zone database comes from the tzdata package: "
                             "pip install tzdata)") from exc
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
