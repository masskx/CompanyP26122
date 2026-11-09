"""Message formats -> Measurement (the only code that knows the wire format).

INGEST_PARSER selects the format:
  simulator     built-in station packet used by the simulator until the real format is known
                topic   <INGEST_TOPIC_PREFIX>/<station_id>
                payload {"ts": <epoch ms | ISO-8601 with zone>,
                         "devices": [{"id": "D01", "dose_rate": 0.085, "unit": "uGy/h",
                                      "status": "normal|h1|h2|fault|offline",
                                      "comm": "ok|interrupted"}, ...],
                         "battery": {"voltage": 13.4, "level": 98, "ac_power": "on|off",
                                     "charging": true},
                         "shelter": {"temperature": 22.5, "humidity": 46, "door": "closed"},
                         "rainfall": 0.0}            every block / field optional
  mapping       a real format described in a JSON file (INGEST_MAPPING_FILE), no code needed;
                see data/mqtt/mapping.example.json
  module:Class  your own parser class, constructed with `settings`, providing parse()

Dose rates are normalised to µGy/h (nGy/h, µGy/h, mGy/h accepted). Sievert units are
rejected: converting Sv to Gy needs a conversion coefficient the plant has to confirm.
Statuses and on/off flags become numeric codes (app/stations/model.py).
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
from app.stations.model import OFF, ON, STATION_NODE, STATUS_CODES, DeviceStatus, Metric
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


class SimulatorFormat:
    """Station packet (one message per station and sampling instant)."""

    def __init__(self, topic_prefix: str = "sim/stations") -> None:
        self.prefix = [p for p in topic_prefix.strip("/").split("/") if p]
        self.partial_rejections: Counter[str] = Counter()

    def encode(self, station_id: str, time: datetime, devices: list[dict],
               battery: dict | None = None, shelter: dict | None = None,
               rainfall: float | None = None) -> RawMessage:
        body: dict[str, Any] = {"ts": int(time.timestamp() * 1000), "devices": devices}
        if battery is not None:
            body["battery"] = battery
        if shelter is not None:
            body["shelter"] = shelter
        if rainfall is not None:
            body["rainfall"] = rainfall
        return RawMessage("/".join([*self.prefix, station_id]),
                          json.dumps(body, ensure_ascii=False).encode())

    def parse(self, message: RawMessage) -> list[Measurement]:
        parts = message.topic.split("/")
        n = len(self.prefix)
        if len(parts) != n + 1 or parts[:n] != self.prefix or not parts[n]:
            raise Rejected("bad_topic", message.topic[:100])
        try:
            body = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Rejected("bad_payload", "not JSON") from exc
        if not isinstance(body, dict):
            raise Rejected("bad_payload", "not an object")
        station, t = parts[n], parse_time(body.get("ts"))
        out: list[Measurement] = []
        errors: list[str] = []

        def add(device: str, metric: Metric, convert, *args) -> None:
            if any(a is None or a is _MISSING for a in args[:1]):
                return
            try:
                out.append(Measurement(station, device, metric.value, t, convert(*args)))
            except Rejected as exc:  # one bad field does not discard the rest
                errors.append(exc.reason)

        for d in body.get("devices") or []:
            if not isinstance(d, dict) or not d.get("id"):
                errors.append("bad_device")
                continue
            dev = str(d["id"])
            add(dev, Metric.DOSE_RATE, dose_rate, d.get("dose_rate"), d.get("unit", "uGy/h"))
            add(dev, Metric.STATUS, status_code, d.get("status"))
            add(dev, Metric.COMM, flag, d.get("comm"))
        b = body.get("battery") or {}
        add(STATION_NODE, Metric.BATTERY_VOLTAGE, number, b.get("voltage"))
        add(STATION_NODE, Metric.BATTERY_LEVEL, number, b.get("level"))
        add(STATION_NODE, Metric.AC_POWER, flag, b.get("ac_power"))
        add(STATION_NODE, Metric.CHARGING, flag, b.get("charging"))
        sh = body.get("shelter") or {}
        add(STATION_NODE, Metric.SHELTER_TEMP, number, sh.get("temperature"))
        add(STATION_NODE, Metric.SHELTER_HUMIDITY, number, sh.get("humidity"))
        add(STATION_NODE, Metric.DOOR, flag, sh.get("door"))
        add(STATION_NODE, Metric.RAINFALL, number, body.get("rainfall"))
        if errors and not out:
            self.partial_rejections.update(errors[1:])
            raise Rejected(errors[0])
        self.partial_rejections.update(errors)
        return out


# ---- mapping parser ------------------------------------------------------------------------

class _Target(BaseModel):
    station_id: str
    device_id: str  # use "_station" for station-level metrics


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
    if s.ingest_parser == "simulator":
        return SimulatorFormat(s.ingest_topic_prefix)
    if s.ingest_parser == "mapping":
        if s.ingest_mapping_file is None:
            raise ValueError("INGEST_PARSER=mapping needs INGEST_MAPPING_FILE")
        return MappingParser.from_file(s.ingest_mapping_file)
    if ":" in s.ingest_parser:
        module, _, cls = s.ingest_parser.partition(":")
        return getattr(importlib.import_module(module), cls)(s)
    raise ValueError(f"unknown INGEST_PARSER {s.ingest_parser!r}")
