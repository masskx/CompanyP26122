"""Wire-format parsers.

The real MQTT topic layout and payload schema are not known yet. `ProvisionalJsonCodec` is
the format the simulator speaks; when the real contract arrives, add a new parser class,
register it in PARSERS and set COLLECTOR_PARSER. Nothing downstream changes.
"""

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from industrial_ai.collector.models import Measurement, RawMessage, RejectedMessage

CANONICAL_DOSE_RATE_UNIT = "µSv/h"

# Factor that converts a value in the given unit to µSv/h.
DOSE_RATE_UNITS: dict[str, float] = {
    "nSv/h": 1e-3,
    "uSv/h": 1.0,
    "µSv/h": 1.0,  # U+00B5 micro sign
    "μSv/h": 1.0,  # U+03BC Greek mu
    "mSv/h": 1e3,
    "Sv/h": 1e6,
}

# metric -> unit table. Metrics not listed here must arrive without a unit (or with "").
METRIC_UNITS: dict[str, dict[str, float]] = {"dose_rate": DOSE_RATE_UNITS}


class MessageParser(Protocol):
    def parse(self, message: RawMessage) -> list[Measurement]:
        """Return measurements, or raise RejectedMessage."""
        ...


def parse_timestamp(value: object) -> datetime:
    """Accept epoch milliseconds/seconds or an ISO-8601 string with an offset."""
    if isinstance(value, bool):
        raise RejectedMessage("bad_timestamp", "boolean timestamp")
    if isinstance(value, int | float):
        if not math.isfinite(value):
            raise RejectedMessage("bad_timestamp", "non-finite timestamp")
        seconds = value / 1000 if abs(value) > 1e11 else value
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RejectedMessage("bad_timestamp", value[:64]) from exc
        if parsed.tzinfo is None:
            raise RejectedMessage("bad_timestamp", "timestamp without timezone")
        return parsed
    raise RejectedMessage("bad_timestamp", f"unsupported type {type(value).__name__}")


def to_canonical(metric: str, value: float, unit: str | None) -> float:
    table = METRIC_UNITS.get(metric)
    if table is None:
        if unit:
            raise RejectedMessage("unknown_unit", f"{metric} does not define units")
        return value
    if unit is None:
        raise RejectedMessage("missing_unit", metric)
    factor = table.get(unit.strip())
    if factor is None:
        raise RejectedMessage("unknown_unit", unit[:32])
    return value * factor


class ProvisionalJsonCodec:
    """Provisional format (simulator <-> collector) until the real contract is known.

    Topic:   <topic_prefix>/<site_id>/<equipment_id>/<metric>
    Payload: {"ts": <epoch ms | ISO-8601>, "value": <number>, "unit": "uSv/h",
              "status": "ok" | "fault" | ...}
    Messages with status other than "ok" are rejected (device-reported fault).
    """

    def __init__(self, topic_prefix: str) -> None:
        self._prefix = topic_prefix.strip("/")
        self._prefix_parts = self._prefix.split("/") if self._prefix else []

    def topic(self, site_id: str, equipment_id: str, metric: str) -> str:
        return "/".join([*self._prefix_parts, site_id, equipment_id, metric])

    def encode(self, site_id: str, equipment_id: str, metric: str, timestamp: datetime,
               value: float, unit: str, status: str = "ok") -> tuple[str, bytes]:
        payload = {"ts": int(timestamp.timestamp() * 1000), "value": value, "unit": unit,
                   "status": status}
        return self.topic(site_id, equipment_id, metric), json.dumps(payload).encode()

    def parse(self, message: RawMessage) -> list[Measurement]:
        parts = message.topic.split("/")
        n = len(self._prefix_parts)
        if len(parts) != n + 3 or parts[:n] != self._prefix_parts:
            raise RejectedMessage("bad_topic", message.topic[:128])
        site_id, equipment_id, metric = parts[n:]

        try:
            body = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RejectedMessage("bad_payload", "not JSON") from exc
        if not isinstance(body, dict):
            raise RejectedMessage("bad_payload", "payload is not an object")

        status = body.get("status", "ok")
        if status != "ok":
            raise RejectedMessage("device_status", str(status)[:32])

        raw_value = body.get("value")
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise RejectedMessage("bad_value", "value is not a number")
        unit = body.get("unit")
        if unit is not None and not isinstance(unit, str):
            raise RejectedMessage("unknown_unit", "unit is not a string")

        return [Measurement(
            site_id=site_id, equipment_id=equipment_id, metric=metric,
            timestamp=parse_timestamp(body.get("ts")),
            value=to_canonical(metric, float(raw_value), unit),
        )]


ParserFactory = Callable[[str], MessageParser]

PARSERS: dict[str, ParserFactory] = {
    "provisional_json": ProvisionalJsonCodec,
}


def build_parser(name: str, topic_prefix: str) -> MessageParser:
    try:
        return PARSERS[name](topic_prefix)
    except KeyError as exc:
        raise ValueError(f"unknown parser {name!r}; available: {sorted(PARSERS)}") from exc
