"""Apache IoTDB adapter for `TimeSeriesStore` (tree model).

IoTDB is a candidate, not a final choice; this adapter is only loaded when
TIMESERIES_BACKEND=iotdb. Install with `pip install -e ".[iotdb]"`.

Path convention (to be agreed with whoever writes the data):
    <root>.<site_id>.<equipment_id>.<metric>   e.g. root.mes.`AS01`.`AS01-ROOM-01`.`temperature`
Timestamps are assumed to be stored with millisecond precision (IoTDB default).
"""

import asyncio
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from industrial_ai.schemas.common import TelemetryPoint, TimeRange
from industrial_ai.storage.base import StorageUnavailableError

logger = logging.getLogger(__name__)

_NODE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_ROOT_RE = re.compile(r"^root(\.[A-Za-z0-9_]+)*$")


def quote_node(name: str) -> str:
    """Quote one path node. Only a safe character set is accepted, so no escaping is needed."""
    if not _NODE_RE.fullmatch(name):
        raise ValueError(f"invalid IoTDB path node: {name!r}")
    return f"`{name}`"


def device_path(root: str, site_id: str, equipment_id: str) -> str:
    if not _ROOT_RE.fullmatch(root):
        raise ValueError(f"invalid IoTDB root path: {root!r}")
    return f"{root}.{quote_node(site_id)}.{quote_node(equipment_id)}"


def to_epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def build_range_query(
    root: str, site_id: str, equipment_id: str, metric: str, time_range: TimeRange
) -> str:
    return (
        f"SELECT {quote_node(metric)} FROM {device_path(root, site_id, equipment_id)} "
        f"WHERE time >= {to_epoch_ms(time_range.start)} AND time <= {to_epoch_ms(time_range.end)} "
        "ORDER BY time ASC"
    )


def _to_float(field: Any) -> float | None:
    if field is None or field.is_null():
        return None
    value = field.get_object_value(field.get_data_type())
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class IoTDBTimeSeriesStore:
    def __init__(self, pool_factory: Callable[[], Any], *, root_path: str) -> None:
        self._pool_factory = pool_factory
        self._pool: Any | None = None
        self._root = root_path

    @classmethod
    def from_settings(
        cls, *, host: str, port: int, user: str, password: str | None, root_path: str,
        pool_size: int, wait_timeout_ms: int,
    ) -> "IoTDBTimeSeriesStore":
        def factory() -> Any:
            try:
                from iotdb.SessionPool import PoolConfig, SessionPool
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise StorageUnavailableError(
                    "apache-iotdb is not installed; install the [iotdb] extra"
                ) from exc
            config = PoolConfig(host=host, port=str(port), user_name=user, password=password)
            return SessionPool(config, pool_size, wait_timeout_ms)

        return cls(factory, root_path=root_path)

    def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = self._pool_factory()
        return self._pool

    def _run_query(self, sql: str) -> list[TelemetryPoint]:
        pool = self._get_pool()
        session = pool.get_session()
        try:
            dataset = session.execute_query_statement(sql)
            points: list[TelemetryPoint] = []
            try:
                while dataset.has_next():
                    row = dataset.next()
                    fields = row.get_fields()
                    points.append(
                        TelemetryPoint(
                            timestamp=datetime.fromtimestamp(row.get_timestamp() / 1000, UTC),
                            value=_to_float(fields[0]) if fields else None,
                        )
                    )
            finally:
                dataset.close_operation_handle()
            return points
        finally:
            pool.put_back(session)

    async def query(
        self, site_id: str, equipment_id: str, metric: str, time_range: TimeRange
    ) -> list[TelemetryPoint]:
        sql = build_range_query(self._root, site_id, equipment_id, metric, time_range)
        try:
            return await asyncio.to_thread(self._run_query, sql)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            logger.warning("iotdb query failed", extra={"fields": {"error": type(exc).__name__}})
            raise StorageUnavailableError("time-series query failed") from exc

    async def ping(self) -> None:
        def _ping() -> None:
            pool = self._get_pool()
            session = pool.get_session()
            try:
                session.execute_query_statement("SHOW VERSION").close_operation_handle()
            finally:
                pool.put_back(session)

        try:
            await asyncio.to_thread(_ping)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError("time-series store unreachable") from exc

    async def close(self) -> None:
        if self._pool is not None:
            await asyncio.to_thread(self._pool.close)
            self._pool = None
