from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from industrial_ai.schemas.common import ApiModel, HealthLevel, TimeRange


class AnalysisType(StrEnum):
    ISOLATION_ROOM = "isolation_room"
    BACKUP_POWER = "backup_power"
    GENERAL = "general"


class AnalysisStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (AnalysisStatus.COMPLETED, AnalysisStatus.PARTIAL, AnalysisStatus.FAILED)


class AnalysisOptions(ApiModel):
    include_report: bool = False


class AnalysisCreate(ApiModel):
    site_id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    equipment_ids: list[str] = Field(min_length=1)
    analysis_type: AnalysisType
    time_range: TimeRange
    options: AnalysisOptions = Field(default_factory=AnalysisOptions)


class AnalysisAccepted(ApiModel):
    analysis_id: str
    status: AnalysisStatus


class Evidence(ApiModel):
    source: str
    equipment_id: str
    metric: str
    time_range: str
    observation: str


class Recommendation(ApiModel):
    priority: Literal["low", "medium", "high"]
    equipment_id: str
    action: str
    rationale: str
    # Recommendations are advisory only: they never trigger device control or work orders.
    requires_human_confirmation: Literal[True] = True


class DataGap(ApiModel):
    equipment_id: str
    metric: str | None = None
    reason: Literal["no_data", "low_completeness", "threshold_not_configured", "no_metrics"]
    detail: str


class ModelInfo(ApiModel):
    provider: str  # deepseek (dev/test only) | vllm
    name: str
    precision: str
    revision: str


class ErrorInfo(ApiModel):
    code: str
    message: str
    retryable: bool = False


class AnalysisResult(ApiModel):
    analysis_id: str
    status: AnalysisStatus
    analysis_type: AnalysisType
    site_id: str
    equipment_ids: list[str]
    time_range: TimeRange
    health_level: HealthLevel | None = None
    risk_summary: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    data_gaps: list[DataGap] = Field(default_factory=list)
    # Data-coverage score from the rule engine (not a model probability).
    confidence: float | None = None
    created_at: datetime
    generated_at: datetime | None = None
    # None when the text comes from deterministic rules only (no model, or model output
    # rejected). The health level is always decided by the rules.
    model: ModelInfo | None = None
    error: ErrorInfo | None = None
