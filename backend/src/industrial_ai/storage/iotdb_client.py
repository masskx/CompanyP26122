"""Apache IoTDB adapter for `TimeSeriesStore` (tree model).

Used when TIMESERIES_BACKEND=iotdb (the default). Writes go through
`industrial_ai.collector.writer.IoTDBWriter`, using the same path convention.

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
from typing import TYPE_CHECKING, Any

from industrial_ai.schemas.common import TelemetryPoint, TimeRange
from industrial_ai.storage.base import StorageUnavailableError

if TYPE_CHECKING:
    from industrial_ai.core.config import Settings

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


def pool_factory_from_settings(settings: "Settings") -> Callable[[], Any]:
    """Lazily create a SessionPool (no connection is made until first use)."""

    def factory() -> Any:
        from iotdb.SessionPool import PoolConfig, SessionPool

        password = settings.iotdb_password.get_secret_value() if settings.iotdb_password else None
        config = PoolConfig(host=settings.iotdb_host, port=str(settings.iotdb_port),
                            user_name=settings.iotdb_user, password=password,
                            enable_redirection=settings.iotdb_enable_redirection)
        return SessionPool(config, settings.iotdb_pool_size, settings.iotdb_wait_timeout_ms)

    return factory


def ensure_schema(settings: "Settings", series: list[tuple[str, str, str]]) -> dict[str, int]:
    """Create the database and DOUBLE time series for (site_id, equipment_id, metric).

    Idempotent. Needed when the server disables automatic schema creation; harmless
    otherwise. Returns counts of created / existing series.
    """
    pool = pool_factory_from_settings(settings)()
    session = pool.get_session()
    created = existing = 0
    try:
        try:
            session.execute_non_query_statement(f"CREATE DATABASE {settings.iotdb_root_path}")
        except Exception as exc:  # already exists
            if "already" not in str(exc).lower():
                raise
        for site_id, equipment_id, metric in series:
            path = f"{device_path(settings.iotdb_root_path, site_id, equipment_id)}." \
                   f"{quote_node(metric)}"
            try:
                session.execute_non_query_statement(
                    f"CREATE TIMESERIES {path} WITH DATATYPE=DOUBLE, ENCODING=GORILLA, "
                    "COMPRESSOR=LZ4")
                created += 1
            except Exception as exc:
                if "already" not in str(exc).lower():
                    raise
                existing += 1
    finally:
        pool.put_back(session)
        pool.close()
    return {"created": created, "existing": existing}


def delete_series(settings: "Settings", series: list[tuple[str, str, str]]) -> int:
    """Delete the given time series (data and schema). Returns how many were deleted."""
    pool = pool_factory_from_settings(settings)()
    session = pool.get_session()
    deleted = 0
    try:
        for site_id, equipment_id, metric in series:
            path = f"{device_path(settings.iotdb_root_path, site_id, equipment_id)}." \
                   f"{quote_node(metric)}"
            try:
                session.execute_non_query_statement(f"DELETE TIMESERIES {path}")
                deleted += 1
            except Exception as exc:
                if "not" not in str(exc).lower():  # "does not exist" / "not matched"
                    raise
    finally:
        pool.put_back(session)
        pool.close()
    return deleted


def count_points(settings: "Settings", site_id: str, equipment_id: str, metric: str
                 ) -> tuple[int, int | None]:
    """(number of points, latest timestamp in ms) for one series; (0, None) if absent."""
    pool = pool_factory_from_settings(settings)()
    session = pool.get_session()
    try:
        device = device_path(settings.iotdb_root_path, site_id, equipment_id)
        column = quote_node(metric)
        ds = session.execute_query_statement(f"SELECT count({column}) FROM {device}")
        count = 0
        if ds.has_next():
            field = ds.next().get_fields()[0]
            count = 0 if field.is_null() else int(field.get_object_value(field.get_data_type()))
        ds.close_operation_handle()
        latest = None
        ds = session.execute_query_statement(
            f"SELECT {column} FROM {device} ORDER BY time DESC LIMIT 1")
        if ds.has_next():
            latest = ds.next().get_timestamp()
        ds.close_operation_handle()
        return count, latest
    except Exception as exc:
        if "does not exist" in str(exc).lower() or "not exist" in str(exc).lower():
            return 0, None
        raise
    finally:
        pool.put_back(session)
        pool.close()


class IoTDBTimeSeriesStore:
    def __init__(self, pool_factory: Callable[[], Any], *, root_path: str) -> None:
        self._pool_factory = pool_factory
        self._pool: Any | None = None
        self._root = root_path

    @classmethod
    def from_settings(cls, settings: "Settings") -> "IoTDBTimeSeriesStore":
        return cls(pool_factory_from_settings(settings), root_path=settings.iotdb_root_path)

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
