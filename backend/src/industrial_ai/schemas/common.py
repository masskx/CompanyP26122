from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthLevel(StrEnum):
    NORMAL = "normal"
    WATCH = "watch"
    INSPECT = "inspect"
    INSUFFICIENT_DATA = "insufficient_data"


class TimeRange(ApiModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _check_order(self) -> "TimeRange":
        if self.start >= self.end:
            raise ValueError("time_range.start must be earlier than time_range.end")
        return self

    def label(self) -> str:
        return f"{self.start.isoformat()}/{self.end.isoformat()}"

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


class TelemetryPoint(ApiModel):
    timestamp: datetime
    value: float | None
