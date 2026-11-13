"""Apache IoTDB access (tree model).

Layout:  <IOTDB_ROOT>.<site_id>.<equipment_id>.<metric>
         e.g. root.mes.`AS01`.`AS01-DET-01`.`dose_rate`, root.mes.`AS01`.`AS01-BAT-01`.`ac_power`
         consumption watermark: root.mes.`_sys`.`ingest`.`watermark`
Values are DOUBLE (dose rate in µGy/h; statuses / flags as codes, see app/stations/model.py),
timestamps in milliseconds (UTC).

The IoTDB Python client is synchronous; every call runs in a worker thread.
"""

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.config import Settings

_NODE = re.compile(r"^[A-Za-z0-9_\-]+$")


class TimeSeriesError(Exception):
    """IoTDB unreachable or a query failed."""


@dataclass(frozen=True, slots=True)
class Point:
    time: datetime
    value: float | None


@dataclass(frozen=True, slots=True)
class Bucket:
    time: datetime
    avg: float | None
    min: float | None
    max: float | None
    count: int


@dataclass(frozen=True, slots=True)
class Measurement:
    site: str
    device_id: str   # equipment id
    metric: str
    time: datetime
    value: float


# Consumption watermark of the MQTT ingester (highest source_sequence fully written),
# stored as its own series so the API process can read what the ingester process wrote.
WATERMARK_SERIES = ("_sys", "ingest", "watermark")


class TimeSeriesStore(Protocol):
    async def ping(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, batch: list[Measurement]) -> None: ...
    async def query(self, site: str, device_id: str, metric: str, start: datetime,
                    end: datetime, limit: int | None = None) -> list[Point]: ...
    async def aggregate(self, site: str, device_id: str, metric: str, start: datetime,
                        end: datetime, interval_seconds: int) -> list[Bucket]: ...
    async def latest(self, site: str, device_id: str, metric: str) -> Point | None: ...
    async def ensure_schema(self, series: list[tuple[str, str, str]]) -> dict[str, int]: ...
    async def delete(self, series: list[tuple[str, str, str]]) -> int: ...
    async def purge(self) -> None: ...


def node(name: str) -> str:
    if not _NODE.fullmatch(name):
        raise ValueError(f"invalid IoTDB path node: {name!r}")
    return f"`{name}`"


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _float(field: Any) -> float | None:
    if field is None or field.is_null():
        return None
    value = field.get_object_value(field.get_data_type())
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class IoTDB:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._pool: Any | None = None
        self._limit: asyncio.Semaphore | None = None
        self._limit_loop: Any = None

    def device_path(self, site: str, device_id: str) -> str:
        return f"{self._s.iotdb_root}.{node(site)}.{node(device_id)}"

    # ---- plumbing ------------------------------------------------------------------------

    def _get_pool(self) -> Any:
        if self._pool is None:
            from iotdb.SessionPool import PoolConfig, SessionPool

            config = PoolConfig(host=self._s.iotdb_host, port=str(self._s.iotdb_port),
                                user_name=self._s.iotdb_user,
                                password=self._s.iotdb_password.get_secret_value(),
                                enable_redirection=False)  # single node; avoids bad redirects
            self._pool = SessionPool(config, self._s.iotdb_pool_size, 3000)
        return self._pool

    def _with_session(self, fn: Any) -> Any:
        pool = self._get_pool()
        session = pool.get_session()
        try:
            return fn(session)
        finally:
            pool.put_back(session)

    def _limiter(self) -> asyncio.Semaphore:
        """At most IOTDB_POOL_SIZE calls in flight: callers wait here (async) instead of
        timing out inside the pool's blocking get_session()."""
        loop = asyncio.get_running_loop()
        if self._limit is None or self._limit_loop is not loop:
            self._limit, self._limit_loop = asyncio.Semaphore(self._s.iotdb_pool_size), loop
        return self._limit

    async def _run(self, fn: Any) -> Any:
        try:
            async with self._limiter():
                return await asyncio.to_thread(self._with_session, fn)
        except ValueError:
            raise
        except Exception as exc:
            raise TimeSeriesError(f"IoTDB error: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _rows(session: Any, sql: str, convert: Any = lambda r: r) -> list[Any]:
        """Convert each row while iterating: the client reuses the RowRecord object, so rows
        must not be kept and read later (they would all show the last row's values)."""
        ds = session.execute_query_statement(sql)
        rows = []
        try:
            while ds.has_next():
                rows.append(convert(ds.next()))
        finally:
            ds.close_operation_handle()
        return rows

    async def close(self) -> None:
        if self._pool is not None:
            await asyncio.to_thread(self._pool.close)
            self._pool = None

    # ---- reads ---------------------------------------------------------------------------

    async def ping(self) -> None:
        await self._run(lambda s: self._rows(s, "SHOW VERSION"))

    async def query(self, site: str, device_id: str, metric: str, start: datetime,
                    end: datetime, limit: int | None = None) -> list[Point]:
        sql = (f"SELECT {node(metric)} FROM {self.device_path(site, device_id)} "
               f"WHERE time >= {ms(start)} AND time <= {ms(end)} ORDER BY time ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"

        def run(session: Any) -> list[Point]:
            return self._rows(session, sql, lambda r: Point(
                datetime.fromtimestamp(r.get_timestamp() / 1000, UTC), _float(r.get_fields()[0])))

        return await self._run(run)

    async def aggregate(self, site: str, device_id: str, metric: str, start: datetime,
                        end: datetime, interval_seconds: int) -> list[Bucket]:
        m = node(metric)
        sql = (f"SELECT avg({m}), min_value({m}), max_value({m}), count({m}) "
               f"FROM {self.device_path(site, device_id)} "
               f"GROUP BY ([{ms(start)}, {ms(end)}), {int(interval_seconds) * 1000}ms)")

        def bucket(r: Any) -> Bucket:
            f = r.get_fields()
            return Bucket(datetime.fromtimestamp(r.get_timestamp() / 1000, UTC), _float(f[0]),
                          _float(f[1]), _float(f[2]), int(_float(f[3]) or 0))

        def run(session: Any) -> list[Bucket]:
            return self._rows(session, sql, bucket)

        return await self._run(run)

    async def latest(self, site: str, device_id: str, metric: str) -> Point | None:
        sql = (f"SELECT {node(metric)} FROM {self.device_path(site, device_id)} "
               "ORDER BY time DESC LIMIT 1")

        def run(session: Any) -> Point | None:
            rows = self._rows(session, sql, lambda r: Point(
                datetime.fromtimestamp(r.get_timestamp() / 1000, UTC), _float(r.get_fields()[0])))
            return rows[0] if rows else None

        return await self._run(run)

    # ---- writes and schema ---------------------------------------------------------------

    async def write(self, batch: list[Measurement]) -> None:
        if not batch:
            return
        await self._run(lambda s: s.insert_records(
            [self.device_path(m.site, m.device_id) for m in batch],
            [ms(m.time) for m in batch],
            [[m.metric] for m in batch],
            [[4] for _ in batch],  # TSDataType.DOUBLE
            [[m.value] for m in batch],
        ))

    async def ensure_schema(self, series: list[tuple[str, str, str]]) -> dict[str, int]:
        """Create the database and DOUBLE series (idempotent)."""
        def run(session: Any) -> dict[str, int]:
            result = {"created": 0, "existing": 0}
            try:
                session.execute_non_query_statement(f"CREATE DATABASE {self._s.iotdb_root}")
            except Exception as exc:
                if "already" not in str(exc).lower():
                    raise
            for site, device_id, metric in series:
                path = f"{self.device_path(site, device_id)}.{node(metric)}"
                try:
                    session.execute_non_query_statement(
                        f"CREATE TIMESERIES {path} WITH DATATYPE=DOUBLE, ENCODING=GORILLA, "
                        "COMPRESSOR=LZ4")
                    result["created"] += 1
                except Exception as exc:
                    if "already" not in str(exc).lower():
                        raise
                    result["existing"] += 1
            return result

        return await self._run(run)

    async def delete(self, series: list[tuple[str, str, str]]) -> int:
        def run(session: Any) -> int:
            deleted = 0
            for site, device_id, metric in series:
                try:
                    session.execute_non_query_statement(
                        f"DELETE TIMESERIES {self.device_path(site, device_id)}.{node(metric)}")
                    deleted += 1
                except Exception as exc:
                    if "not" not in str(exc).lower():
                        raise
            return deleted

        return await self._run(run)


    async def purge(self) -> None:
        """Delete every series under IOTDB_ROOT (e.g. old demo data)."""
        def run(session: Any) -> None:
            try:
                session.execute_non_query_statement(f"DELETE TIMESERIES {self._s.iotdb_root}.**")
            except Exception as exc:
                if "not" not in str(exc).lower():
                    raise

        await self._run(run)


class MemoryTimeSeries:
    """In-memory stand-in with the same interface (tests and database-free demos)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str, str], dict[int, float]] = {}

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def write(self, batch: list[Measurement]) -> None:
        for m in batch:
            self.data.setdefault((m.site, m.device_id, m.metric), {})[ms(m.time)] = m.value

    async def query(self, site, device_id, metric, start, end, limit=None) -> list[Point]:
        series = self.data.get((site, device_id, metric), {})
        points = [Point(datetime.fromtimestamp(t / 1000, UTC), v) for t, v in sorted(series.items())
                  if ms(start) <= t <= ms(end)]
        return points[:limit] if limit else points

    async def aggregate(self, site, device_id, metric, start, end, interval_seconds
                        ) -> list[Bucket]:
        step = interval_seconds * 1000
        buckets: dict[int, list[float]] = {}
        for p in await self.query(site, device_id, metric, start, end):
            if ms(p.time) < ms(end) and p.value is not None:
                key = ms(start) + (ms(p.time) - ms(start)) // step * step
                buckets.setdefault(key, []).append(p.value)
        out = []
        for key in range(ms(start), ms(end), step):
            vals = buckets.get(key, [])
            out.append(Bucket(datetime.fromtimestamp(key / 1000, UTC),
                              sum(vals) / len(vals) if vals else None,
                              min(vals) if vals else None, max(vals) if vals else None, len(vals)))
        return out

    async def latest(self, site, device_id, metric) -> Point | None:
        series = self.data.get((site, device_id, metric))
        if not series:
            return None
        t = max(series)
        return Point(datetime.fromtimestamp(t / 1000, UTC), series[t])

    async def ensure_schema(self, series) -> dict[str, int]:
        return {"created": 0, "existing": len(series)}

    async def delete(self, series) -> int:
        return sum(self.data.pop(key, None) is not None for key in series)

    async def purge(self) -> None:
        self.data.clear()
