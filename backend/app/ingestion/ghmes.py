"""ghmes/signal: the main server's signal snapshots (the format the MQTT administrator sent).

Topic   ghmes/signal (QoS 1)
Payload a JSON array with one element per monitored object ("signal"):
    [{"signalCode": "sig-001", "signalName": "AS01-三废区", "bizSystem": "固定子站",
      "dataTime": "2026-11-23 20:16:16",                      # local time, no time zone
      "channels": [{"deviceCode": "station1", "deviceName": "AS01-三废区", "channelNo": 2,
                    "alias": "", "label": "伽马瞬时值", "fieldName": "gamma_instant",
                    "kind": "value", "unit": "μGy/h", "value": 0.086, "status": "",
                    "commStatus": "通讯正常"}, ...]},
     {"signalCode": "sig-016", "signalName": "环境监测车", "bizSystem": "环境监测车", ...},
     {"signalCode": "sig-017", "signalName": "气象塔", "bizSystem": "气象塔", ...}]

How it is read is described in a profile (INGEST_GHMES_FILE, default data/mqtt/ghmes.json),
so a new field, code or object needs no code change:
  * station signals (bizSystem in `station_systems`, e.g. 固定子站): the station id comes
    from `station_map` (signalCode -> station id) or from the start of signalName
    ("AS01-三废区" -> AS01). `channels` maps each fieldName to one or more targets: a station
    unit (detector / backup_power / environment), a metric and, for status codes, a map
    vendor code -> value. Unknown codes and unmapped fields are counted, not stored.
  * auxiliary signals (`auxiliary`, keyed by signalCode or bizSystem, e.g. 环境监测车,
    气象塔): every channel is stored as reported under <source_id>.<equipment_id>.<fieldName>
    (no unit conversion; the units are declared in STATIONS_FILE "auxiliary_sources").
  * communication (`comm_ok` / `comm_fault`, commStatus texts): a channel with a
    communication fault carries placeholder values (0), which are NOT stored. The unit gets
    comm = interrupted, and its status metrics (detector status, station_status) = offline.
dataTime has no time zone: it is read in the profile's `timezone` (default
ANALYSIS_TIMEZONE). The feed has no source_sequence, so the consumption watermark is the
newest dataTime consumed, in epoch milliseconds; the main server sends mqtt_watermark as the
same epoch-ms value or as an ISO time (its data_cutoff).
"""

import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings
from app.ingestion.parsers import RawMessage, Rejected, convert, number, parse_time
from app.stations.model import (
    FIELD_PATTERN,
    METRIC_TYPE,
    OFF,
    ON,
    STATUS_CODES,
    STATUS_METRICS,
    DeviceStatus,
    Metric,
    Station,
)
from app.timeseries.iotdb import Measurement

UnitKind = Literal["detector", "backup_power", "environment"]
_FIELD = re.compile(FIELD_PATTERN)
_OFFLINE = float(STATUS_CODES[DeviceStatus.OFFLINE])


class ChannelTarget(BaseModel):
    unit: UnitKind
    metric: Metric
    # vendor value -> stored value (normal|h1|h2|fault|offline for statuses, on/off for flags)
    codes: dict[str, str] | None = None

    @model_validator(mode="after")
    def _check(self) -> "ChannelTarget":
        if METRIC_TYPE[self.metric].value != self.unit:
            raise ValueError(f"{self.metric} belongs to the {METRIC_TYPE[self.metric]} unit")
        return self


class AuxiliaryRoute(BaseModel):
    source_id: str
    equipment_id: str | None = None  # default <source_id>-01 (as in AuxiliarySource)

    @property
    def equipment(self) -> str:
        return self.equipment_id or f"{self.source_id}-01"


class GhmesProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str = "ghmes/signal"
    timezone: str | None = None
    station_systems: list[str] = Field(default_factory=lambda: ["固定子站"])
    station_map: dict[str, str] = Field(default_factory=dict)  # signalCode -> station id
    station_id_pattern: str = r"^(?P<station>[A-Za-z]{1,4}\d{1,4})(?:[-_\s]|$)"
    equipment: dict[UnitKind, str] = Field(default_factory=lambda: {
        "detector": "{site}-DET-01", "backup_power": "{site}-BAT-01",
        "environment": "{site}-ENV-01"})
    channels: dict[str, list[ChannelTarget]] = Field(default_factory=dict)
    ignore_fields: list[str] = Field(default_factory=list)
    auxiliary: dict[str, AuxiliaryRoute] = Field(default_factory=dict)
    comm_ok: list[str] = Field(default_factory=lambda: ["通讯正常", "正常", "在线"])
    comm_fault: list[str] = Field(default_factory=lambda: ["通讯故障", "通讯中断", "离线"])


def _topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


def _code_key(value: Any) -> str:
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        text = value.strip()
        try:
            f = float(text)
            return str(int(f)) if math.isfinite(f) and f.is_integer() else text
        except ValueError:
            return text
    return str(value)


class GhmesFormat:
    def __init__(self, profile: GhmesProfile, timezone: str = "Asia/Shanghai") -> None:
        self.profile = profile
        self.tz = ZoneInfo(profile.timezone or timezone)
        self._pattern = re.compile(profile.station_id_pattern)
        self._ok = {s.strip() for s in profile.comm_ok}
        self._fault = {s.strip() for s in profile.comm_fault}
        self.partial_rejections: Counter[str] = Counter()
        self.last_sequence: int | None = None  # newest dataTime of the last message, epoch ms

    @classmethod
    def from_file(cls, path: Path, timezone: str = "Asia/Shanghai") -> "GhmesFormat":
        if not path.is_file():
            raise FileNotFoundError(f"ghmes profile not found: {path} (INGEST_GHMES_FILE)")
        return cls(GhmesProfile.model_validate(json.loads(path.read_text(encoding="utf-8"))),
                   timezone)

    @classmethod
    def from_settings(cls, s: Settings) -> "GhmesFormat":
        return cls.from_file(s.ingest_ghmes_file, s.analysis_timezone)

    # -- parsing -------------------------------------------------------------------------------

    def parse(self, message: RawMessage) -> list[Measurement]:
        self.last_sequence = None
        if not _topic_matches(self.profile.topic, message.topic):
            raise Rejected("bad_topic", message.topic[:100])
        try:
            body = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Rejected("bad_payload", "not JSON") from exc
        signals = body if isinstance(body, list) else [body] if isinstance(body, dict) else None
        if signals is None:
            raise Rejected("bad_payload", "not a list of signals")
        out: list[Measurement] = []
        errors: list[str] = []
        for sig in signals:
            try:
                out.extend(self._signal(sig))
            except Rejected as exc:  # one bad signal does not discard the others
                errors.append(exc.reason)
        if errors and not out:
            self.partial_rejections.update(errors[1:])
            raise Rejected(errors[0])
        self.partial_rejections.update(errors)
        return out

    def _time(self, raw: Any) -> datetime:
        if isinstance(raw, str) and raw.strip() and not raw.strip().isdigit():
            try:
                t = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise Rejected("bad_timestamp", raw[:40]) from exc
            return t if t.tzinfo else t.replace(tzinfo=self.tz)
        return parse_time(raw)

    def _signal(self, sig: Any) -> list[Measurement]:
        if not isinstance(sig, dict):
            raise Rejected("bad_payload", "signal is not an object")
        t = self._time(sig.get("dataTime"))
        stamp = int(t.timestamp() * 1000)
        if self.last_sequence is None or stamp > self.last_sequence:
            self.last_sequence = stamp
        channels = sig.get("channels")
        if not isinstance(channels, list):
            raise Rejected("bad_payload", "channels is not a list")
        code, biz = str(sig.get("signalCode") or ""), str(sig.get("bizSystem") or "")
        p = self.profile
        if code in p.station_map:
            return self._station(p.station_map[code], t, channels)
        route = p.auxiliary.get(code)
        if route is None and biz in p.station_systems:
            name = str(sig.get("signalName") or "")
            match = self._pattern.match(name)
            if not match:
                raise Rejected("unmapped_signal", f"{code} {name}"[:60])
            return self._station(match.group("station"), t, channels)
        route = route or p.auxiliary.get(biz)
        if route is None:
            raise Rejected("unmapped_signal", f"{code} {biz}"[:60])
        return self._auxiliary(route, t, channels)

    def _comm(self, raw: Any) -> bool | None:
        text = str(raw or "").strip()
        if text in self._fault:
            return False
        if text in self._ok:
            return True
        if text:
            self.partial_rejections["unknown_comm_status"] += 1
        return None  # unknown: keep the values, report no comm state

    def _value(self, target: ChannelTarget, raw: Any, unit: Any) -> float:
        if target.codes is not None:
            mapped = target.codes.get(_code_key(raw))
            if mapped is None:
                raise Rejected("unknown_code", f"{target.metric}={raw}"[:40])
            return convert(target.metric.value, mapped, None)
        return convert(target.metric.value, raw, unit)

    def _station(self, site: str, t: datetime, channels: list) -> list[Measurement]:
        p = self.profile
        eq = {k: v.format(site=site) for k, v in p.equipment.items()}
        out: list[Measurement] = []
        comm: dict[str, bool] = {}
        for ch in channels:
            if not isinstance(ch, dict):
                self.partial_rejections["bad_channel"] += 1
                continue
            field = str(ch.get("fieldName") or "")
            targets = p.channels.get(field)
            if targets is None:
                if field not in p.ignore_fields:
                    self.partial_rejections["unmapped_field"] += 1
                continue
            state = self._comm(ch.get("commStatus"))
            for target in targets:
                if state is not None:
                    comm[target.unit] = comm.get(target.unit, True) and state
                if state is False:  # placeholder value: store "offline", not the 0
                    if target.metric in STATUS_METRICS:
                        out.append(Measurement(site, eq[target.unit], target.metric.value, t,
                                               _OFFLINE))
                    continue
                value = ch.get("value")
                if value is None or value == "":
                    self.partial_rejections["missing_value"] += 1
                    continue
                try:
                    out.append(Measurement(site, eq[target.unit], target.metric.value, t,
                                           self._value(target, value, ch.get("unit"))))
                except Rejected as exc:
                    self.partial_rejections[exc.reason] += 1
        if "detector" in comm:
            out.append(Measurement(site, eq["detector"], Metric.COMM.value, t,
                                   ON if comm["detector"] else OFF))
        if not out:
            raise Rejected("no_mapped_channels", site)
        return out

    def _auxiliary(self, route: AuxiliaryRoute, t: datetime, channels: list
                   ) -> list[Measurement]:
        out: list[Measurement] = []
        comm: bool | None = None
        for ch in channels:
            if not isinstance(ch, dict):
                self.partial_rejections["bad_channel"] += 1
                continue
            field = str(ch.get("fieldName") or "")
            if not _FIELD.fullmatch(field) or field == "comm":
                self.partial_rejections["bad_field"] += 1
                continue
            state = self._comm(ch.get("commStatus"))
            if state is not None:
                comm = (comm if comm is not None else True) and state
            if state is False or ch.get("value") in (None, ""):
                continue
            try:
                out.append(Measurement(route.source_id, route.equipment, field, t,
                                       number(ch.get("value"))))
            except Rejected as exc:
                self.partial_rejections[exc.reason] += 1
        if comm is not None:
            out.append(Measurement(route.source_id, route.equipment, "comm", t,
                                   ON if comm else OFF))
        if not out:
            raise Rejected("no_mapped_channels", route.source_id)
        return out

    # -- encoding (simulator / tests) --------------------------------------------------------

    def encode_signal(self, station: Station, time: datetime,
                      readings: list[tuple[str, dict[str, Any], dict[str, str]]]) -> dict:
        """One station signal from simulated readings (equipment id, points, units), using
        the profile backwards. Metrics without a channel in the profile are not sent."""
        p = self.profile
        by_kind: dict[str, dict[str, Any]] = {}
        units: dict[str, str] = {}
        for eq, points, u in readings:
            kind = station.equipment_type(eq)
            if kind is not None:
                by_kind[kind.value] = points
                units.update(u)
        det = by_kind.get("detector")
        det_down = det is not None and str(det.get("comm", "ok")) in ("interrupted", "0",
                                                                         "False", "false")
        ok_text = p.comm_ok[0] if p.comm_ok else ""
        fault_text = p.comm_fault[0] if p.comm_fault else ""
        site_code = next((c for c, s in p.station_map.items() if s == station.station_id),
                         f"sig-{station.station_id}")
        channels = []
        for field, targets in p.channels.items():
            for target in targets:
                points = by_kind.get(target.unit)
                if points is None:
                    continue
                down = target.unit == "detector" and det_down
                metric = target.metric.value
                if not down and metric not in points:
                    continue
                value: Any = 0
                if not down:
                    value = points[metric]
                    if target.codes is not None:
                        stored = convert(metric, value, None)
                        value = next((int(k) if k.lstrip("-").isdigit() else k
                                      for k, v in target.codes.items()
                                      if convert(metric, v, None) == stored), None)
                        if value is None:
                            continue
                channels.append({
                    "deviceCode": f"station-{station.station_id}",
                    "deviceName": f"{station.station_id}-{station.name}",
                    "channelNo": len(channels) + 1, "alias": "", "label": field,
                    "fieldName": field, "kind": "status" if target.codes is not None
                    else "value", "unit": units.get(metric, ""), "value": value,
                    "status": "", "commStatus": fault_text if down else ok_text})
                break
        return {"signalCode": site_code, "signalName": f"{station.station_id}-{station.name}",
                "bizSystem": p.station_systems[0] if p.station_systems else "固定子站",
                "dataTime": time.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S"),
                "channels": channels}

    def encode_readings(self, station: Station, time: datetime,
                        readings: list[tuple[str, dict[str, Any], dict[str, str]]]
                        ) -> list[RawMessage]:
        topic = self.profile.topic.replace("/#", "").replace("+", "x")
        body = [self.encode_signal(station, time, readings)]
        return [RawMessage(topic, json.dumps(body, ensure_ascii=False).encode())]
