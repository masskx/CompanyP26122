"""Configurable parser: describe a real MQTT message format in JSON instead of writing code.

Example (see examples/mqtt/mapping.example.json):

    {
      "topic_pattern": "^plant/(?P<site_id>[^/]+)/radiation/(?P<device>[^/]+)$",
      "records_path": null,                 # or "readings" when one message carries a list
      "fields": {
        "device": "deviceId",               # dotted path in the record; omit to use the topic
        "timestamp": "ts",
        "value": "data.doseRate",
        "unit": "data.unit",                # omit to use default_unit
        "status": "status",                 # omit if the format has no status
        "metric": null                      # omit to use default_metric
      },
      "timestamp_format": "auto",           # auto | epoch_ms | epoch_s | iso8601
      "default_unit": "nSv/h",
      "default_metric": "dose_rate",
      "ok_status": ["ok", "normal", 0],
      "device_map": {"RM-001": {"site_id": "AS01", "equipment_id": "AS01-ISO-RM-01"}},
      "unmapped_devices": "reject"          # reject | passthrough
    }

Resolution of (site_id, equipment_id) per record:
    1. device_map[device] if present
    2. otherwise (unmapped_devices=passthrough): site_id from the topic group / "site_id"
       field, equipment_id = device
Values are converted to the canonical unit (µSv/h for dose_rate) exactly like the
provisional format, so the IoTDB layout and everything downstream stay unchanged.
"""

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from industrial_ai.collector.models import Measurement, RawMessage, RejectedMessage
from industrial_ai.collector.parser import parse_timestamp, to_canonical

_MISSING = object()


class DeviceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_id: str
    equipment_id: str
    metric: str | None = None


class FieldPaths(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str | None = None
    site_id: str | None = None
    timestamp: str = "ts"
    value: str = "value"
    unit: str | None = None
    status: str | None = None
    metric: str | None = None


class MappingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, alias="_comment")
    topic_pattern: str = ".*"
    records_path: str | None = None
    fields: FieldPaths = Field(default_factory=FieldPaths)
    timestamp_format: Literal["auto", "epoch_ms", "epoch_s", "iso8601"] = "auto"
    default_unit: str | None = None
    default_metric: str = "dose_rate"
    ok_status: list[str | int | bool] = Field(default_factory=lambda: ["ok"])
    device_map: dict[str, DeviceTarget] = Field(default_factory=dict)
    unmapped_devices: Literal["reject", "passthrough"] = "reject"


def get_path(obj: Any, path: str) -> Any:
    """Dotted lookup: "data.doseRate", "readings.0.value". Returns _MISSING if absent."""
    current = obj
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


class MappingParser:
    def __init__(self, config: MappingConfig) -> None:
        self.config = config
        self._topic = re.compile(config.topic_pattern)
        self._partial: Counter[str] = Counter()

    def drain_rejections(self) -> Counter[str]:
        """Rejected records inside otherwise valid multi-record messages (for stats)."""
        out, self._partial = self._partial, Counter()
        return out

    @classmethod
    def from_file(cls, path: Path) -> "MappingParser":
        return cls(MappingConfig.model_validate(json.loads(path.read_text(encoding="utf-8"))))

    def parse(self, message: RawMessage) -> list[Measurement]:
        c = self.config
        match = self._topic.match(message.topic)
        if match is None:
            raise RejectedMessage("bad_topic", message.topic[:128])
        topic_groups = match.groupdict()
        try:
            body = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RejectedMessage("bad_payload", "not JSON") from exc

        records = body if c.records_path is None else get_path(body, c.records_path)
        if records is _MISSING:
            raise RejectedMessage("bad_payload", f"missing {c.records_path}")
        if not isinstance(records, list):
            records = [records]
        # One bad record must not discard the good ones in the same message.
        measurements, rejected = [], []
        for record in records:
            try:
                measurements.append(self._record(record, topic_groups))
            except RejectedMessage as exc:
                rejected.append(exc)
        if not measurements and rejected:
            self._partial.update(r.reason for r in rejected[1:])
            raise rejected[0]
        self._partial.update(r.reason for r in rejected)
        return measurements

    def _record(self, record: Any, topic_groups: dict[str, str]) -> Measurement:
        c, f = self.config, self.config.fields
        if not isinstance(record, dict):
            raise RejectedMessage("bad_payload", "record is not an object")

        if f.status is not None:
            status = get_path(record, f.status)
            if status is not _MISSING and status not in c.ok_status:
                raise RejectedMessage("device_status", str(status)[:32])

        device = get_path(record, f.device) if f.device else topic_groups.get("device", _MISSING)
        if device is _MISSING or device in (None, ""):
            raise RejectedMessage("missing_device", "no device identifier")
        device = str(device)
        target = c.device_map.get(device)
        if target is not None:
            site_id, equipment_id, metric = target.site_id, target.equipment_id, target.metric
        elif c.unmapped_devices == "passthrough":
            site = get_path(record, f.site_id) if f.site_id else topic_groups.get("site_id",
                                                                                  _MISSING)
            if site is _MISSING or site in (None, ""):
                raise RejectedMessage("missing_site", f"device {device}")
            site_id, equipment_id, metric = str(site), device, None
        else:
            raise RejectedMessage("unmapped_device", device[:64])

        if metric is None:
            raw_metric = get_path(record, f.metric) if f.metric else topic_groups.get("metric")
            metric = c.default_metric if raw_metric in (None, _MISSING, "") else str(raw_metric)

        raw_value = get_path(record, f.value)
        if isinstance(raw_value, str):
            try:
                raw_value = float(raw_value)
            except ValueError:
                raw_value = _MISSING
        if raw_value is _MISSING or isinstance(raw_value, bool) or \
                not isinstance(raw_value, int | float):
            raise RejectedMessage("bad_value", "value missing or not a number")

        unit = get_path(record, f.unit) if f.unit else _MISSING
        unit = c.default_unit if unit in (_MISSING, None, "") else unit
        if unit is not None and not isinstance(unit, str):
            raise RejectedMessage("unknown_unit", "unit is not a string")

        return Measurement(site_id=site_id, equipment_id=equipment_id, metric=metric,
                           timestamp=self._timestamp(get_path(record, f.timestamp)),
                           value=to_canonical(metric, float(raw_value), unit))

    def _timestamp(self, raw: Any) -> datetime:
        if raw is _MISSING:
            raise RejectedMessage("bad_timestamp", "missing timestamp")
        fmt = self.config.timestamp_format
        if isinstance(raw, str) and fmt in ("epoch_ms", "epoch_s") and raw.isdigit():
            raw = int(raw)
        if fmt == "epoch_ms" and isinstance(raw, int | float) and not isinstance(raw, bool):
            return datetime.fromtimestamp(raw / 1000, UTC)
        if fmt == "epoch_s" and isinstance(raw, int | float) and not isinstance(raw, bool):
            return datetime.fromtimestamp(raw, UTC)
        if fmt == "iso8601" and not isinstance(raw, str):
            raise RejectedMessage("bad_timestamp", "expected ISO-8601 string")
        return parse_timestamp(raw)
