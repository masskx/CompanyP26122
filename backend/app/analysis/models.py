"""Analysis results: the contract between the nightly batch, openGauss and the GET API.

batch   one run of the daily analysis for one calendar day (date + version + revision)
record  one per abnormal device (detector) or per station battery (backup_power);
        normal devices produce no record
event   one abnormal interval inside a record (a record may hold several)
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Level(StrEnum):
    WATCH = "watch"                          # H1 / 需关注
    INSPECT = "inspect"                      # H2, device fault / 需检修
    INSUFFICIENT_DATA = "insufficient_data"  # offline, interrupted, gaps / 数据缺失


SEVERITY = {Level.INSPECT: 3, Level.WATCH: 2, Level.INSUFFICIENT_DATA: 1}
LEVEL_LABELS = {Level.WATCH: "需关注", Level.INSPECT: "需检修",
                Level.INSUFFICIENT_DATA: "数据缺失"}


def worst(levels: list[Level]) -> Level:
    return max(levels, key=lambda lv: SEVERITY[lv])


class Source(StrEnum):
    """Where a determination comes from (audit)."""

    SOURCE_REPORTED = "source_reported"  # status reported by the data source / device
    RULE = "rule"                        # computed by our rules from the samples
    DATA_QUALITY = "data_quality"        # data-quality detection (gaps, no data, stuck)


class Category(StrEnum):
    DOSE = "dose"
    DEVICE = "device"
    DATA_QUALITY = "data_quality"
    BATTERY = "battery"


class EquipmentType(StrEnum):
    DETECTOR = "detector"
    BACKUP_POWER = "backup_power"


class BatchStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"   # some stations / devices failed; the other records are valid
    FAILED = "failed"


class Comparison(BaseModel):
    status: Literal["available", "unavailable", "not_applicable"]
    reason: str | None = None
    reference_start: datetime | None = None
    reference_end: datetime | None = None
    samples: int | None = None
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    delta_mean: float | None = None      # event mean - reference mean
    delta_pct: float | None = None       # relative to the reference mean, in %
    days_available: int | None = None    # previous_7_days only


class Event(BaseModel):
    event_id: str
    record_id: str
    code: str                   # E1, E2 ... unique within the record (used by the LLM)
    kind: str
    title: str
    level: Level
    source: Source
    category: Category
    start: datetime
    end: datetime
    duration_s: float
    open_at_start: bool = False
    open_at_end: bool = False
    sample_count: int = 0
    expected_samples: int = 0
    completeness: float | None = None
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    std: float | None = None
    rule: dict = Field(default_factory=dict)          # rule id, description, thresholds
    comparisons: dict[str, Comparison] = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)       # AC power, rainfall, shelter
    corroborated_by_source: bool | None = None
    values: list[tuple[datetime, float]] | None = None  # samples in the interval
    values_truncated: bool = False


class EvidenceItem(BaseModel):
    event_ids: list[str]        # event codes (E1 ...)
    observation: str
    metric: str | None = None   # filled by code from the referenced events
    time_range: str | None = None
    source: str | None = None


class Recommendation(BaseModel):
    priority: Literal["high", "medium", "low"]
    action: str
    rationale: str
    event_ids: list[str] = Field(default_factory=list)
    requires_human_confirmation: bool = True


class Explanation(BaseModel):
    summary: str
    details: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    method: Literal["llm", "template", "template_fallback"]
    model: str | None = None
    error: str | None = None             # why the LLM output was rejected (fallback)
    confidence: float                    # computed by code, never by the model
    confidence_factors: dict = Field(default_factory=dict)
    generated_at: datetime


class Record(BaseModel):
    record_id: str
    batch_id: str
    analysis_date: date
    station_id: str
    station_name: str
    equipment_type: EquipmentType
    device_id: str              # "BATTERY" for the station battery
    device_name: str
    level: Level
    level_label: str = ""       # always derived from level
    title: str
    sources: list[Source]
    event_count: int
    data_status: Literal["complete", "partial", "missing"]
    day_stats: dict = Field(default_factory=dict)
    explanation: Explanation | None = None
    created_at: datetime
    events: list[Event] | None = None  # record detail only

    @model_validator(mode="after")
    def _label(self) -> "Record":
        self.level_label = LEVEL_LABELS[self.level]
        return self


class Batch(BaseModel):
    batch_id: str
    analysis_date: date
    version: str
    revision: int
    status: BatchStatus
    trigger: Literal["schedule", "catchup", "manual", "retry"]
    attempts: int
    timezone: str
    window_start: datetime
    window_end: datetime
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: dict = Field(default_factory=dict)
    config_gaps: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    config_snapshot: dict | None = None  # batch detail only
    is_current: bool = False
