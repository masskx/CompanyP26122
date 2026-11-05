"""Measurement sinks."""

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from typing import Any, Protocol

from industrial_ai.collector.models import Measurement
from industrial_ai.schemas.common import TelemetryPoint
from industrial_ai.storage.base import StorageUnavailableError
from industrial_ai.storage.iotdb_client import device_path, to_epoch_ms
from industrial_ai.storage.memory import InMemoryTimeSeriesStore

logger = logging.getLogger(__name__)

_TS_DOUBLE = 4  # iotdb.utils.IoTDBConstants.TSDataType.DOUBLE


class MeasurementWriter(Protocol):
    async def write(self, batch: list[Measurement]) -> None:
        """Persist the batch atomically or raise StorageUnavailableError."""
        ...

    async def close(self) -> None: ...


class StdoutWriter:
    """Dry run: print what would be stored (one JSON object per measurement)."""

    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stdout
        self.count = 0

    async def write(self, batch: list[Measurement]) -> None:
        for m in batch:
            self._stream.write(json.dumps({
                "site_id": m.site_id, "equipment_id": m.equipment_id, "metric": m.metric,
                "timestamp": m.timestamp.isoformat(), "value": m.value}, ensure_ascii=False)
                + "\n")
        self._stream.flush()
        self.count += len(batch)

    async def close(self) -> None:
        return None


class InMemoryWriter:
    """Writes into the in-memory store the API reads from (tests / local development)."""

    def __init__(self, store: InMemoryTimeSeriesStore) -> None:
        self._store = store

    async def write(self, batch: list[Measurement]) -> None:
        grouped: dict[tuple[str, str, str], list[TelemetryPoint]] = {}
        for m in batch:
            grouped.setdefault((m.site_id, m.equipment_id, m.metric), []).append(
                TelemetryPoint(timestamp=m.timestamp, value=m.value))
        for (site, equipment, metric), points in grouped.items():
            self._store.insert(site, equipment, metric, points)

    async def close(self) -> None:
        return None


class IoTDBWriter:
    """Tree-model writer: one DOUBLE time series per <root>.<site>.<equipment>.<metric>.

    Relies on IoTDB automatic schema creation (enabled by default). If the production
    server disables it, pre-create the series with the same paths.
    """

    def __init__(self, pool_factory: Callable[[], Any], *, root_path: str) -> None:
        self._pool_factory = pool_factory
        self._pool: Any | None = None
        self._root = root_path

    def _write_sync(self, batch: list[Measurement]) -> None:
        if self._pool is None:
            self._pool = self._pool_factory()
        session = self._pool.get_session()
        try:
            session.insert_records(
                [device_path(self._root, m.site_id, m.equipment_id) for m in batch],
                [to_epoch_ms(m.timestamp) for m in batch],
                [[m.metric] for m in batch],
                [[_TS_DOUBLE] for _ in batch],
                [[m.value] for m in batch],
            )
        finally:
            self._pool.put_back(session)

    async def write(self, batch: list[Measurement]) -> None:
        if not batch:
            return
        try:
            await asyncio.to_thread(self._write_sync, batch)
        except Exception as exc:
            logger.warning("iotdb write failed", extra={"fields": {
                "error": type(exc).__name__, "batch_size": len(batch)}})
            raise StorageUnavailableError("time-series write failed") from exc

    async def close(self) -> None:
        if self._pool is not None:
            await asyncio.to_thread(self._pool.close)
            self._pool = None
