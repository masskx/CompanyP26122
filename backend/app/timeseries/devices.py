"""Device catalog and deterministic dose-rate assessment.

The catalog (DEVICES_FILE, default data/devices.json) lists every monitoring device, its
metrics and thresholds. Thresholds may be omitted (e.g. real dose-rate limits not provided
yet): such metrics are reported as `insufficient_data` instead of guessing.

Assessment rules per metric over a time window:
  no valid samples                                  -> insufficient_data
  no thresholds configured                          -> insufficient_data
  >= N consecutive samples beyond an alarm limit    -> inspect
  any alarm-limit sample, or >= N consecutive
  samples beyond a warning limit                    -> watch
  completeness below the minimum (no breach)        -> insufficient_data
  otherwise                                         -> normal
Device level = worst metric (inspect > watch > insufficient_data > normal).
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.timeseries.iotdb import Point


class Level(StrEnum):
    NORMAL = "normal"
    WATCH = "watch"
    INSPECT = "inspect"
    INSUFFICIENT_DATA = "insufficient_data"


_SEVERITY = [Level.INSPECT, Level.WATCH, Level.INSUFFICIENT_DATA, Level.NORMAL]


class Metric(BaseModel):
    name: str = Field(default="dose_rate", pattern=r"^[A-Za-z0-9_]+$")
    unit: str = "µSv/h"
    interval_seconds: int | None = Field(default=None, gt=0)  # expected sampling interval
    warn_high: float | None = None
    alarm_high: float | None = None
    warn_low: float | None = None
    alarm_low: float | None = None

    @property
    def has_thresholds(self) -> bool:
        return any(v is not None for v in (self.warn_high, self.alarm_high,
                                           self.warn_low, self.alarm_low))


class Device(BaseModel):
    device_id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    site: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    name: str
    type: str = "radiation_monitor"
    location: str | None = None
    metrics: list[Metric] = Field(default_factory=lambda: [Metric()])
    simulation: dict[str, Any] | None = None  # profile for the simulator (optional)

    def metric(self, name: str) -> Metric | None:
        return next((m for m in self.metrics if m.name == name), None)


class DeviceCatalog:
    def __init__(self, devices: list[Device]) -> None:
        self._devices = {d.device_id: d for d in devices}
        if len(self._devices) != len(devices):
            raise ValueError("duplicate device_id in device catalog")

    @classmethod
    def load(cls, path: Path) -> "DeviceCatalog":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([Device.model_validate(d) for d in raw.get("devices", [])])

    def all(self, site: str | None = None, type: str | None = None) -> list[Device]:
        return [d for d in self._devices.values()
                if (site is None or d.site == site) and (type is None or d.type == type)]

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def series(self) -> list[tuple[str, str, str]]:
        return [(d.site, d.device_id, m.name) for d in self._devices.values() for m in d.metrics]


@dataclass
class MetricAssessment:
    metric: str
    unit: str
    level: Level
    samples: int
    completeness: float | None
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    last: float | None = None
    last_time: datetime | None = None
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["level"] = self.level.value
        d["last_time"] = self.last_time.isoformat() if self.last_time else None
        return d


def _outside(v: float, low: float | None, high: float | None) -> bool:
    return (low is not None and v < low) or (high is not None and v > high)


def _longest_run(values: list[float | None], pred) -> int:
    best = run = 0
    for v in values:
        if v is None:
            continue
        run = run + 1 if pred(v) else 0
        best = max(best, run)
    return best


def assess_metric(metric: Metric, points: list[Point], start: datetime, end: datetime, *,
                  min_completeness: float = 0.8, consecutive: int = 3) -> MetricAssessment:
    values = [p.value for p in sorted(points, key=lambda p: p.time)]
    valid = [v for v in values if v is not None]
    completeness = None
    if metric.interval_seconds:
        expected = max(1.0, (end - start).total_seconds() / metric.interval_seconds)
        completeness = round(min(1.0, len(valid) / expected), 4)
    a = MetricAssessment(metric.name, metric.unit, Level.INSUFFICIENT_DATA, len(valid),
                         completeness)
    if not valid:
        a.findings.append("时间范围内无有效数据")
        return a
    last = max((p for p in points if p.value is not None), key=lambda p: p.time)
    a.min, a.max = min(valid), max(valid)
    a.mean, a.last, a.last_time = round(sum(valid) / len(valid), 6), last.value, last.time

    low_completeness = completeness is not None and completeness < min_completeness
    if low_completeness:
        a.findings.append(f"数据完整率 {completeness:.0%} 低于要求 {min_completeness:.0%}")
    if not metric.has_thresholds:
        a.findings.append("未配置判定阈值，无法判断是否异常")
        return a

    def is_alarm(v: float) -> bool:
        return _outside(v, metric.alarm_low, metric.alarm_high)

    def is_warn(v: float) -> bool:
        return is_alarm(v) or _outside(v, metric.warn_low, metric.warn_high)

    alarm_run = _longest_run(values, is_alarm)
    warn_run = _longest_run(values, is_warn)
    alarm_points = sum(1 for v in valid if is_alarm(v))
    if alarm_run >= consecutive:
        a.level = Level.INSPECT
        a.findings.append(f"连续 {alarm_run} 个样本超出报警限值")
    elif alarm_points or warn_run >= consecutive:
        a.level = Level.WATCH
        if alarm_points:
            a.findings.append(f"{alarm_points} 个样本超出报警限值（未连续 {consecutive} 个）")
        if warn_run >= consecutive:
            a.findings.append(f"连续 {warn_run} 个样本超出预警限值")
    elif not low_completeness:
        a.level = Level.NORMAL
    return a


def worst(levels: list[Level]) -> Level:
    present = set(levels)
    return next((lv for lv in _SEVERITY if lv in present), Level.INSUFFICIENT_DATA)
