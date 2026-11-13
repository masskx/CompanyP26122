"""Analysis request / result contract (docs/项目规划/设计文档.md §4.2). snake_case; the main
server maps the result to camelCase for the frontend (AiAnalysis in
frontend/src/features/maintenance/types.ts)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AnalysisType = Literal["detector", "backup_power", "general"]
HealthLevel = Literal["normal", "watch", "inspect", "insufficient_data"]


class Window(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _check(self) -> "Window":
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("window times must include a time zone")
        if self.start >= self.end:
            raise ValueError("window start must be before end")
        return self


class AnalysisRequest(BaseModel):
    analysis_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w.:\-]+$")
    site_id: str = Field(min_length=1, max_length=64)
    equipment_ids: list[str] = Field(default_factory=list, max_length=20)
    analysis_type: AnalysisType
    observation_window: Window
    history_lookback_days: int = Field(default=90, ge=0, le=365)
    data_cutoff: datetime | None = None
    mqtt_watermark: str | None = Field(default=None, max_length=32)

    @field_validator("mqtt_watermark")
    @classmethod
    def _numeric(cls, v: str | None) -> str | None:
        if v is not None and not v.strip().isdigit():
            raise ValueError("mqtt_watermark must be a non-negative integer sequence")
        return v.strip() if v else v


class Finding(BaseModel):
    rule_id: str
    code: str
    severity: Literal["watch", "inspect"]
    metric: str
    observed: float | None = None
    baseline: float | None = None
    change_percent: float | None = None
    window: str
    message: str
    source_refs: list[str] = Field(default_factory=list)


class Action(BaseModel):
    action_code: str
    priority: Literal["primary", "secondary"]
    text: str
    requires_human_confirmation: bool = True


class Limitation(BaseModel):
    code: str
    text: str
    affects: str


class DataSnapshot(BaseModel):
    mqtt_watermark: str | None
    window: Window
    data_as_of: datetime | None
    sample_count: int
    completeness: float
    missing_rate: float


class ModelVersion(BaseModel):
    name: str
    precision: str
    revision: str


class Versions(BaseModel):
    rule_set: str
    action_catalog: str
    prompt: str
    model: ModelVersion


class AnalysisResult(BaseModel):
    analysis_id: str
    status: Literal["completed"] = "completed"
    health_level: HealthLevel
    explanation: str
    explanation_source: Literal["model", "template"]
    findings: list[Finding] = Field(default_factory=list, max_length=3)
    actions: list[Action] = Field(default_factory=list, max_length=3)
    limitations: list[Limitation] = Field(default_factory=list, max_length=1)
    data_snapshot: DataSnapshot
    versions: Versions
    generated_at: datetime

    @model_validator(mode="after")
    def _contract(self) -> "AnalysisResult":
        if self.health_level == "insufficient_data":
            if self.actions or not self.limitations or self.explanation_source != "template":
                raise ValueError("insufficient_data: no actions, >= 1 limitation, template text")
        elif self.health_level in ("watch", "inspect"):
            if not self.findings or not self.actions or self.actions[0].priority != "primary":
                raise ValueError(f"{self.health_level}: findings and a primary action required")
        return self


class AnalysisError(Exception):
    """Mapped to an HTTP error with a sanitised body."""

    def __init__(self, status: int, code: str, message: str, retryable: bool = False,
                 **extra) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable
        self.extra = extra
