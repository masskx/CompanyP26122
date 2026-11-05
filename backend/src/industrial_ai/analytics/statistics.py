"""Descriptive statistics over telemetry series."""

from dataclasses import dataclass
from datetime import datetime

from industrial_ai.schemas.common import TelemetryPoint


@dataclass(frozen=True)
class SeriesStats:
    total_points: int
    valid_points: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    last_value: float | None
    last_timestamp: datetime | None


def summarize(points: list[TelemetryPoint]) -> SeriesStats:
    valid = [p for p in points if p.value is not None]
    if not valid:
        return SeriesStats(len(points), 0, None, None, None, None, None)
    values = [p.value for p in valid if p.value is not None]
    last = max(valid, key=lambda p: p.timestamp)
    return SeriesStats(
        total_points=len(points),
        valid_points=len(valid),
        minimum=min(values),
        maximum=max(values),
        mean=sum(values) / len(values),
        last_value=last.value,
        last_timestamp=last.timestamp,
    )


def completeness(
    valid_points: int, expected_interval_seconds: int | None, duration_seconds: float
) -> float | None:
    """Fraction of expected samples that are present; None if the interval is unknown."""
    if expected_interval_seconds is None or duration_seconds <= 0:
        return None
    expected = max(1.0, duration_seconds / expected_interval_seconds)
    return min(1.0, valid_points / expected)
