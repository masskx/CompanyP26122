from datetime import datetime

from pydantic import Field, model_validator

from industrial_ai.schemas.common import ApiModel, HealthLevel


class MetricThresholds(ApiModel):
    """Bounds supplied by the plant. Any bound may be absent."""

    warn_low: float | None = None
    warn_high: float | None = None
    alarm_low: float | None = None
    alarm_high: float | None = None

    @model_validator(mode="after")
    def _check_any(self) -> "MetricThresholds":
        if all(v is None for v in (self.warn_low, self.warn_high, self.alarm_low, self.alarm_high)):
            raise ValueError("at least one bound is required; use thresholds=null if unknown")
        return self


class MetricDefinition(ApiModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    display_name: str | None = None
    unit: str | None = None
    # None means the plant has not provided limits yet; rules then report a data gap
    # instead of guessing.
    thresholds: MetricThresholds | None = None
    # Expected sampling interval, used for data-completeness checks. None = unknown.
    expected_interval_seconds: int | None = Field(default=None, gt=0)


class Equipment(ApiModel):
    equipment_id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    site_id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    name: str
    category: str
    metrics: list[MetricDefinition] = Field(default_factory=list)


class EquipmentHealth(ApiModel):
    equipment_id: str
    name: str
    category: str
    health_level: HealthLevel
    data_completeness: float | None = None
    last_seen: datetime | None = None
    reasons: list[str] = Field(default_factory=list)


class HealthOverview(ApiModel):
    site_id: str
    category: str | None
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    counts: dict[HealthLevel, int]
    items: list[EquipmentHealth]
