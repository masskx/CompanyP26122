"""Message formats -> Measurement (the only code that knows the wire format).

INGEST_PARSER selects the format:
  simulator     built-in format used by the simulator (and until the real one is known)
                topic   <INGEST_TOPIC_PREFIX>/<site>/<device_id>/<metric>
                payload {"ts": <epoch ms|ISO-8601>, "value": 0.51, "unit": "uSv/h", "status": "ok"}
  mapping       the real format described in a JSON file (INGEST_MAPPING_FILE), no code needed;
                see data/mqtt_mapping.example.json
  module:Class  your own parser class, constructed with `settings`, providing parse()
Dose rates are converted to µSv/h, so storage and everything downstream never change.
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
from app.timeseries.iotdb import Measurement

DOSE_RATE_UNITS = {"nSv/h": 1e-3, "uSv/h": 1.0, "µSv/h": 1.0, "μSv/h": 1.0, "mSv/h": 1e3,
                   "Sv/h": 1e6}
_MISSING = object()


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


def to_usv_per_hour(metric: str, value: Any, unit: Any) -> float:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            value = None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise Rejected("bad_value", "not a number")
    if metric != "dose_rate":
        return float(value)
    if not isinstance(unit, str) or unit.strip() not in DOSE_RATE_UNITS:
        raise Rejected("unknown_unit", str(unit)[:20])
    return float(value) * DOSE_RATE_UNITS[unit.strip()]


class SimulatorFormat:
    def __init__(self, topic_prefix: str = "sim/radiation") -> None:
        self.prefix = [p for p in topic_prefix.strip("/").split("/") if p]

    def encode(self, site: str, device_id: str, metric: str, time: datetime, value: float,
               unit: str = "uSv/h") -> RawMessage:
        topic = "/".join([*self.prefix, site, device_id, metric])
        body = {"ts": int(time.timestamp() * 1000), "value": value, "unit": unit, "status": "ok"}
        return RawMessage(topic, json.dumps(body).encode())

    def parse(self, message: RawMessage) -> list[Measurement]:
        parts = message.topic.split("/")
        n = len(self.prefix)
        if len(parts) != n + 3 or parts[:n] != self.prefix:
            raise Rejected("bad_topic", message.topic[:100])
        try:
            body = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Rejected("bad_payload", "not JSON") from exc
        if not isinstance(body, dict):
            raise Rejected("bad_payload", "not an object")
        if body.get("status", "ok") != "ok":
            raise Rejected("device_status", str(body.get("status"))[:20])
        site, device_id, metric = parts[n:]
        return [Measurement(site, device_id, metric, parse_time(body.get("ts")),
                            to_usv_per_hour(metric, body.get("value"), body.get("unit")))]


# ---- mapping parser ------------------------------------------------------------------------

class _Target(BaseModel):
    site: str
    device_id: str
    metric: str | None = None


class _Fields(BaseModel):
    device: str | None = None
    site: str | None = None
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
    ok_status: list[str | int | bool] = Field(default_factory=lambda: ["ok"])
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
    """Real MQTT format described in JSON (see data/mqtt_mapping.example.json):
    topic regex with named groups (site, device, metric), dotted field paths, optional list
    of readings per message, default unit, accepted status values, vendor-id -> device map."""

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
        out, errors = [], []
        for record in records if isinstance(records, list) else [records]:
            try:
                out.append(self._record(record, match.groupdict()))
            except Rejected as exc:  # one bad reading does not discard the others
                errors.append(exc)
        if errors and not out:
            self.partial_rejections.update(e.reason for e in errors[1:])
            raise errors[0]
        self.partial_rejections.update(e.reason for e in errors)
        return out

    def _record(self, r: Any, groups: dict[str, str]) -> Measurement:
        c, f = self.config, self.config.fields
        if not isinstance(r, dict):
            raise Rejected("bad_payload", "reading is not an object")
        if f.status and (st := get_path(r, f.status)) is not _MISSING and st not in c.ok_status:
            raise Rejected("device_status", str(st)[:20])
        device = get_path(r, f.device) if f.device else groups.get("device", _MISSING)
        if device in (_MISSING, None, ""):
            raise Rejected("missing_device")
        target = c.device_map.get(str(device))
        if target:
            site, device_id, metric = target.site, target.device_id, target.metric
        elif c.unmapped_devices == "passthrough":
            site = get_path(r, f.site) if f.site else groups.get("site", _MISSING)
            if site in (_MISSING, None, ""):
                raise Rejected("missing_site", str(device))
            site, device_id, metric = str(site), str(device), None
        else:
            raise Rejected("unmapped_device", str(device)[:60])
        if metric is None:
            raw = get_path(r, f.metric) if f.metric else groups.get("metric")
            metric = c.default_metric if raw in (None, _MISSING, "") else str(raw)
        unit = get_path(r, f.unit) if f.unit else _MISSING
        unit = c.default_unit if unit in (_MISSING, None, "") else unit
        return Measurement(site, device_id, metric, parse_time(get_path(r, f.timestamp)),
                           to_usv_per_hour(metric, get_path(r, f.value), unit))


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
