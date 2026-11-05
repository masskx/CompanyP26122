"""Data passed between collector stages.

RawMessage is what any transport delivers (MQTT topic + payload bytes). Measurement is the
normalised form written to the time-series store. Only the parser knows the wire format,
so swapping the simulator for real MQTT, or the provisional format for the real one,
never touches validation, batching or storage.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RawMessage:
    topic: str
    payload: bytes
    received_at: datetime


@dataclass(frozen=True, slots=True)
class Measurement:
    site_id: str
    equipment_id: str
    metric: str
    timestamp: datetime  # timezone-aware
    value: float  # in the metric's canonical unit (dose_rate: µSv/h)


class RejectedMessage(Exception):  # noqa: N818 - a domain outcome, not a crash
    """A message or measurement that is deliberately not stored. `reason` is a short code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
