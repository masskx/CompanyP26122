"""In-memory store implementations for development and tests.

Data is lost on restart. Replace with real backends once the database products are chosen.
"""

import asyncio
import bisect
from collections import defaultdict
from typing import Any

from industrial_ai.schemas.analysis import AnalysisResult
from industrial_ai.schemas.common import TelemetryPoint, TimeRange
from industrial_ai.schemas.events import EventType, StreamEvent
from industrial_ai.storage.base import AnalysisRecord

SeriesKey = tuple[str, str, str]


class InMemoryTimeSeriesStore:
    def __init__(self) -> None:
        self._series: dict[SeriesKey, list[TelemetryPoint]] = defaultdict(list)

    def insert(
        self, site_id: str, equipment_id: str, metric: str, points: list[TelemetryPoint]
    ) -> None:
        series = self._series[(site_id, equipment_id, metric)]
        series.extend(points)
        series.sort(key=lambda p: p.timestamp)

    async def query(
        self, site_id: str, equipment_id: str, metric: str, time_range: TimeRange
    ) -> list[TelemetryPoint]:
        series = self._series.get((site_id, equipment_id, metric), [])
        stamps = [p.timestamp for p in series]
        lo = bisect.bisect_left(stamps, time_range.start)
        hi = bisect.bisect_right(stamps, time_range.end)
        return list(series[lo:hi])

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None


class InMemoryAnalysisStore:
    def __init__(self) -> None:
        self._records: dict[str, AnalysisRecord] = {}
        self._by_key: dict[str, str] = {}
        self._events: dict[str, list[StreamEvent]] = defaultdict(list)
        self._conditions: dict[str, asyncio.Condition] = {}
        self._lock = asyncio.Lock()

    def _condition(self, analysis_id: str) -> asyncio.Condition:
        cond = self._conditions.get(analysis_id)
        if cond is None:
            cond = self._conditions[analysis_id] = asyncio.Condition()
        return cond

    async def create(self, record: AnalysisRecord) -> tuple[AnalysisRecord, bool]:
        async with self._lock:
            key = record.idempotency_key
            if key is not None and key in self._by_key:
                return self._records[self._by_key[key]], False
            analysis_id = record.result.analysis_id
            self._records[analysis_id] = record
            if key is not None:
                self._by_key[key] = analysis_id
            return record, True

    async def get(self, analysis_id: str) -> AnalysisRecord | None:
        return self._records.get(analysis_id)

    async def save_result(self, result: AnalysisResult) -> None:
        record = self._records[result.analysis_id]
        record.result = result

    async def append_event(
        self, analysis_id: str, event: EventType, data: dict[str, Any]
    ) -> StreamEvent:
        cond = self._condition(analysis_id)
        async with cond:
            events = self._events[analysis_id]
            item = StreamEvent(seq=len(events) + 1, event=event, data=data)
            events.append(item)
            cond.notify_all()
            return item

    async def wait_for_events(
        self, analysis_id: str, after_seq: int, wait_seconds: float
    ) -> list[StreamEvent]:
        cond = self._condition(analysis_id)
        async with cond:
            def pending() -> list[StreamEvent]:
                return [e for e in self._events.get(analysis_id, []) if e.seq > after_seq]

            if not pending():
                try:
                    await asyncio.wait_for(cond.wait_for(lambda: bool(pending())), wait_seconds)
                except TimeoutError:
                    return []
            return pending()
