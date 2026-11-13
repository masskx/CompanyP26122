"""Message formats -> Measurement (the only code that knows the wire format).

INGEST_PARSER selects the format:
  telemetry_v1  the main server's MQTT contract (default; the simulator publishes it too)
                topic   maintenance/v1/{site_id}/{equipment_id}/telemetry   (QoS 1)
                payload {"schema_version": "1", "message_id": "...", "source_sequence": 18427,
                         "site_id": "AS01", "equipment_id": "AS01-DET-01",
                         "event_time": "2026-10-15T10:24:00+08:00",
                         "points": [{"metric": "dose_rate", "value": 0.286, "unit": "uGy/h",
                                     "quality": "good"}, ...]}
                A single point may also be sent inline (metric / value / unit / quality next to
                event_time). Status values: normal | h1 | h2 | fault | offline; flags:
                on/off, true/false, 1/0. quality "bad" / "invalid" points are dropped.
  mapping       another format described in a JSON file (INGEST_MAPPING_FILE), no code needed
  module:Class  your own parser class, constructed with `settings`, providing parse()

Dose rates are normalised to µGy/h (nGy/h, µGy/h, mGy/h accepted). Sievert units are
rejected: converting Sv to Gy needs a conversion coefficient the plant has to confirm.
`source_sequence` feeds the consumption watermark the analysis API waits for.
"""

import importlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.config import Settings
from app.stations.model import OFF, ON, STATUS_CODES, DeviceStatus, Metric
from app.timeseries.iotdb import Measurement

DOSE_RATE_UNITS = {"nGy/h": 1e-3, "uGy/h": 1.0, "µGy/h": 1.0, "μGy/h": 1.0, "mGy/h": 1e3}
_MISSING = object()
_TRUE = {"on", "ok", "true", "1", "yes", "open", "normal", "charging", "online"}
_FALSE = {"off", "false", "0", "no", "closed", "close", "lost", "interrupted", "idle",
          "offline", "fault", "down"}


@dataclass(frozen=True, slots=True)
class RawMessage:
    topic: str
    payload: bytes


class Rejected(Exception):  # noqa: N818 - an expected outcome, counted by reason
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


class Parser(Protocol):
    def parse(self, message: RawMessage) -> list[Measurement]: ...


def parse_time(raw: Any) -> datetime:
    if isinstance(raw, bool) or raw is None or raw is _MISSING:
        raise Rejected("bad_timestamp", "missing")
    if isinstance(raw, int | float):
        if not math.isfinite(raw):
            raise Rejected("bad_timestamp", "not finite")
        return datetime.fromtimestamp(raw / 1000 if abs(raw) > 1e11 else raw, UTC)
    if isinstance(raw, str):
        if raw.isdigit():
            return parse_time(int(raw))
        try:
            t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Rejected("bad_timestamp", raw[:40]) from exc
        if t.tzinfo is None:
            raise Rejected("bad_timestamp", "no time zone")
        return t
    raise Rejected("bad_timestamp", type(raw).__name__)


def number(value: Any) -> float:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            value = None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise Rejected("bad_value", "not a number")
    return float(value)


def dose_rate(value: Any, unit: Any) -> float:
    v = number(value)
    if not isinstance(unit, str) or unit.strip() not in DOSE_RATE_UNITS:
        raise Rejected("unknown_unit", str(unit)[:20])
    return v * DOSE_RATE_UNITS[unit.strip()]


def flag(value: Any) -> float:
    if isinstance(value, bool):
        return ON if value else OFF
    if isinstance(value, int | float) and value in (0, 1):
        return float(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return ON
    if text in _FALSE:
        return OFF
    raise Rejected("bad_flag", str(value)[:20])


def status_code(value: Any) -> float:
    try:
        return float(STATUS_CODES[DeviceStatus(str(value).strip().lower())])
    except ValueError as exc:
        raise Rejected("unknown_status", str(value)[:20]) from exc


_GOOD_QUALITY = {"good", "valid", "ok", "uncertain", "有效"}
_BAD_QUALITY = {"bad", "invalid", "无效"}
_FLAG_METRICS = {Metric.COMM, Metric.AC_POWER, Metric.CHARGING, Metric.DOOR}


def convert(metric: str, value: Any, unit: Any) -> float:
    """One reported value -> stored number (µGy/h for dose rates, codes for statuses)."""
    try:
        m = Metric(metric)
    except ValueError as exc:
        raise Rejected("unknown_metric", str(metric)[:40]) from exc
    if m == Metric.STATUS:
        return status_code(value)
    if m in _FLAG_METRICS:
        return flag(value)
    if m in (Metric.DOSE_RATE, Metric.DOSE_RATE_AVG, Metric.DOSE_RATE_STD):
        return dose_rate(value, unit if unit not in (None, "") else "uGy/h")
    return number(value)


class TelemetryV1Format:
    """maintenance/v1/{site_id}/{equipment_id}/telemetry (see module docstring)."""

    def __init__(self, topic_prefix: str = "maintenance/v1") -> None:
        self.prefix = [p for p in topic_prefix.strip("/").split("/") if p]
        self.partial_rejections: Counter[str] = Counter()
        self.last_sequence: int | None = None  # of the last message seen (accepted or not)
        self._seq = 0

    # -- encoding (simulator / tests) --------------------------------------------------------

    def encode(self, site_id: str, equipment_id: str, time: datetime, points: dict[str, Any],
               sequence: int | None = None, units: dict[str, str] | None = None
               ) -> RawMessage:
        if sequence is None:
            self._seq += 1
            sequence = self._seq
        body = {"schema_version": "1",
                "message_id": f"{site_id}:{equipment_id}:{int(time.timestamp() * 1000)}",
                "source_sequence": sequence, "site_id": site_id, "equipment_id": equipment_id,
                "event_time": time.isoformat(),
                "points": [{"metric": k, "value": v, "quality": "good",
                            **({"unit": units[k]} if units and k in units else {})}
                           for k, v in points.items() if v is not None]}
        return RawMessage("/".join([*self.prefix, site_id, equipment_id, "telemetry"]),
                          json.dumps(body, ensure_ascii=False).encode())

    # -- parsing -------------------------------------------------------------------------------

    def parse(self, message: RawMessage) -> list[Measurement]:
        self.last_sequence = None
        parts = message.topic.split("/")
        n = len(self.prefix)
        if len(parts) != n + 3 or parts[:n] != self.prefix or parts[-1] != "telemetry" \
                or not parts[n] or not parts[n + 1]:
            raise Rejected("bad_topic", message.topic[:100])
        site, equipment = parts[n], parts[n + 1]
        try:
            body = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Rejected("bad_payload", "not JSON") from exc
        if not isinstance(body, dict):
            raise Rejected("bad_payload", "not an object")
        seq = body.get("source_sequence")
        if isinstance(seq, str) and seq.isdigit():
            seq = int(seq)
        if isinstance(seq, int) and not isinstance(seq, bool):
            self.last_sequence = seq
        if str(body.get("schema_version", "1")).split(".")[0] != "1":
            raise Rejected("unsupported_schema", str(body.get("schema_version"))[:10])
        if body.get("site_id", site) != site or body.get("equipment_id", equipment) != equipment:
            raise Rejected("topic_mismatch", f"{site}/{equipment}")
        t = parse_time(body.get("event_time", body.get("ts")))
        points = body.get("points")
        if points is None:
            points = [body]
        if not isinstance(points, list):
            raise Rejected("bad_payload", "points is not a list")
        out: list[Measurement] = []
        errors: list[str] = []
        for p in points:
            if not isinstance(p, dict) or not p.get("metric"):
                errors.append("bad_point")
                continue
            quality = str(p.get("quality", "good")).strip().lower()
            if quality in _BAD_QUALITY:
                errors.append("bad_quality")
                continue
            if quality not in _GOOD_QUALITY:
                errors.append("unknown_quality")
                continue
            if p.get("value") is None:
                errors.append("missing_value")
                continue
            try:
                out.append(Measurement(site, equipment, str(p["metric"]),
                                       parse_time(p["event_time"]) if p.get("event_time") else t,
                                       convert(str(p["metric"]), p.get("value"), p.get("unit"))))
            except Rejected as exc:  # one bad point does not discard the others
                errors.append(exc.reason)
        if errors and not out:
            self.partial_rejections.update(errors[1:])
            raise Rejected(errors[0])
        self.partial_rejections.update(errors)
        return out


# ---- mapping parser ------------------------------------------------------------------------

class _Target(BaseModel):
    station_id: str
    device_id: str  # equipment id: detector, <site>-BAT-01 or <site>-ENV-01


class _Fields(BaseModel):
    device: str | None = None
    station: str | None = None
    timestamp: str = "ts"
    value: str = "value"
    unit: str | None = None
    status: str | None = None
    metric: str | None = None


class MappingConfig(BaseModel):
    comment: str | None = Field(default=None, alias="_comment")
    topic_pattern: str = ".*"
    records_path: str | None = None
    fields: _Fields = Field(default_factory=_Fields)
    default_unit: str | None = None
    default_metric: str = "dose_rate"
    # vendor status value -> normal | h1 | h2 | fault | offline (stored, never dropped)
    status_map: dict[str, DeviceStatus] = Field(default_factory=dict)
    device_map: dict[str, _Target] = Field(default_factory=dict)
    unmapped_devices: Literal["reject", "passthrough"] = "reject"


def get_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return _MISSING
    return obj


class MappingParser:
    """Real MQTT format described in JSON (see data/mqtt/mapping.example.json): topic regex
    with named groups (station, device, metric), dotted field paths, optional list of
    readings per message, default unit, vendor status map, vendor-id -> device map."""

    def __init__(self, config: MappingConfig) -> None:
        self.config = config
        self._topic = re.compile(config.topic_pattern)
        self.partial_rejections: Counter[str] = Counter()

    @classmethod
    def from_file(cls, path: Path) -> "MappingParser":
        return cls(MappingConfig.model_validate(json.loads(path.read_text(encoding="utf-8"))))

    def parse(self, message: RawMessage) -> list[Measurement]:
        match = self._topic.match(message.topic)
        if not match:
            raise Rejected("bad_topic", message.topic[:100])
        try:
            body = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Rejected("bad_payload", "not JSON") from exc
        records = body if self.config.records_path is None else \
            get_path(body, self.config.records_path)
        if records is _MISSING:
            raise Rejected("bad_payload", f"missing {self.config.records_path}")
        out: list[Measurement] = []
        errors: list[Rejected] = []
        for record in records if isinstance(records, list) else [records]:
            try:
                out.extend(self._record(record, match.groupdict()))
            except Rejected as exc:  # one bad reading does not discard the others
                errors.append(exc)
        if errors and not out:
            self.partial_rejections.update(e.reason for e in errors[1:])
            raise errors[0]
        self.partial_rejections.update(e.reason for e in errors)
        return out

    def _record(self, r: Any, groups: dict[str, str]) -> list[Measurement]:
        c, f = self.config, self.config.fields
        if not isinstance(r, dict):
            raise Rejected("bad_payload", "reading is not an object")
        device = get_path(r, f.device) if f.device else groups.get("device", _MISSING)
        if device in (_MISSING, None, ""):
            raise Rejected("missing_device")
        target = c.device_map.get(str(device))
        if target:
            station, device_id = target.station_id, target.device_id
        elif c.unmapped_devices == "passthrough":
            station = get_path(r, f.station) if f.station else groups.get("station", _MISSING)
            if station in (_MISSING, None, ""):
                raise Rejected("missing_station", str(device))
            station, device_id = str(station), str(device)
        else:
            raise Rejected("unmapped_device", str(device)[:60])
        t = parse_time(get_path(r, f.timestamp))
        raw_metric = get_path(r, f.metric) if f.metric else groups.get("metric")
        metric = c.default_metric if raw_metric in (None, _MISSING, "") else str(raw_metric)
        out = []
        if f.status and (st := get_path(r, f.status)) not in (_MISSING, None):
            mapped = c.status_map.get(str(st))
            if mapped is None:
                raise Rejected("unknown_status", str(st)[:20])
            out.append(Measurement(station, device_id, Metric.STATUS.value, t,
                                   float(STATUS_CODES[mapped])))
        value = get_path(r, f.value)
        if value not in (_MISSING, None):
            if metric == Metric.DOSE_RATE:
                unit = get_path(r, f.unit) if f.unit else _MISSING
                unit = c.default_unit if unit in (_MISSING, None, "") else unit
                out.append(Measurement(station, device_id, metric, t, dose_rate(value, unit)))
            else:
                out.append(Measurement(station, device_id, metric, t, number(value)))
        if not out:
            raise Rejected("bad_value", "no value and no status")
        return out


def build_parser(s: Settings) -> Parser:
    if s.ingest_parser in ("telemetry_v1", "simulator"):
        return TelemetryV1Format(s.ingest_topic_prefix)
    if s.ingest_parser == "mapping":
        if s.ingest_mapping_file is None:
            raise ValueError("INGEST_PARSER=mapping needs INGEST_MAPPING_FILE")
        return MappingParser.from_file(s.ingest_mapping_file)
    if ":" in s.ingest_parser:
        module, _, cls = s.ingest_parser.partition(":")
        return getattr(importlib.import_module(module), cls)(s)
    raise ValueError(f"unknown INGEST_PARSER {s.ingest_parser!r}")
