"""Format-independent checks applied to every parsed measurement before storage."""

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from industrial_ai.collector.models import Measurement, RejectedMessage

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_METRIC_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Metrics that are physically non-negative.
NON_NEGATIVE_METRICS = frozenset({"dose_rate"})


class MeasurementValidator:
    def __init__(
        self,
        *,
        known_equipment: set[tuple[str, str]] | None = None,
        max_future_skew: timedelta = timedelta(minutes=5),
        max_age: timedelta | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._known = known_equipment
        self._max_future_skew = max_future_skew
        self._max_age = max_age
        self._clock = clock

    def check(self, m: Measurement) -> None:
        if not _ID_RE.fullmatch(m.site_id) or not _ID_RE.fullmatch(m.equipment_id):
            raise RejectedMessage("bad_identifier", f"{m.site_id}/{m.equipment_id}"[:128])
        if not _METRIC_RE.fullmatch(m.metric):
            raise RejectedMessage("bad_identifier", m.metric[:64])
        if self._known is not None and (m.site_id, m.equipment_id) not in self._known:
            raise RejectedMessage("unknown_equipment", f"{m.site_id}/{m.equipment_id}")
        if not math.isfinite(m.value):
            raise RejectedMessage("bad_value", "non-finite value")
        if m.metric in NON_NEGATIVE_METRICS and m.value < 0:
            raise RejectedMessage("bad_value", f"negative {m.metric}")

        now = self._clock()
        if m.timestamp > now + self._max_future_skew:
            raise RejectedMessage("future_timestamp", m.timestamp.isoformat())
        if self._max_age is not None and m.timestamp < now - self._max_age:
            raise RejectedMessage("stale_timestamp", m.timestamp.isoformat())
