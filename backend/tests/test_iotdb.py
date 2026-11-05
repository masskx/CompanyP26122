"""IoTDB adapter tests against a fake session pool (no server required)."""

from datetime import UTC, datetime, timedelta

import pytest

from industrial_ai.schemas.common import TimeRange
from industrial_ai.storage.base import StorageUnavailableError
from industrial_ai.storage.iotdb_client import IoTDBTimeSeriesStore, build_range_query, quote_node

pytestmark = pytest.mark.anyio

START = datetime(2026, 10, 1, tzinfo=UTC)
RANGE = TimeRange(start=START, end=START + timedelta(minutes=2))


class FakeField:
    def __init__(self, value):
        self.value = value

    def is_null(self):
        return self.value is None

    def get_data_type(self):
        return 4

    def get_object_value(self, _data_type):
        return self.value


class FakeRow:
    def __init__(self, ts_ms, value):
        self._ts, self._value = ts_ms, value

    def get_timestamp(self):
        return self._ts

    def get_fields(self):
        return [FakeField(self._value)]


class FakeDataSet:
    def __init__(self, rows):
        self._rows = list(rows)
        self.closed = False

    def has_next(self):
        return bool(self._rows)

    def next(self):
        return self._rows.pop(0)

    def close_operation_handle(self):
        self.closed = True


class FakeSession:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.sql = rows, fail, []

    def execute_query_statement(self, sql):
        self.sql.append(sql)
        if self.fail:
            raise RuntimeError("connection refused")
        return FakeDataSet(self.rows)


class FakePool:
    def __init__(self, session):
        self.session, self.returned, self.closed = session, 0, False

    def get_session(self):
        return self.session

    def put_back(self, _session):
        self.returned += 1

    def close(self):
        self.closed = True


def test_quote_node_rejects_injection():
    assert quote_node("AS01-ROOM-01") == "`AS01-ROOM-01`"
    for bad in ["a`b", "a.b", "a b", "x;DELETE", ""]:
        with pytest.raises(ValueError):
            quote_node(bad)


def test_build_range_query():
    sql = build_range_query("root.mes", "AS01", "AS01-ROOM-01", "temperature", RANGE)
    start_ms = int(START.timestamp() * 1000)
    assert sql == (
        "SELECT `temperature` FROM root.mes.`AS01`.`AS01-ROOM-01` "
        f"WHERE time >= {start_ms} AND time <= {start_ms + 120_000} ORDER BY time ASC"
    )


async def test_query_parses_rows_and_returns_session():
    t0 = int(START.timestamp() * 1000)
    session = FakeSession([FakeRow(t0, 1.5), FakeRow(t0 + 60_000, None),
                           FakeRow(t0 + 120_000, float("nan"))])
    pool = FakePool(session)
    store = IoTDBTimeSeriesStore(lambda: pool, root_path="root.mes")

    points = await store.query("AS01", "E1", "m1", RANGE)

    assert [p.value for p in points] == [1.5, None, None]
    assert points[0].timestamp == START
    assert pool.returned == 1
    await store.close()
    assert pool.closed


async def test_query_failure_maps_to_storage_error():
    pool = FakePool(FakeSession([], fail=True))
    store = IoTDBTimeSeriesStore(lambda: pool, root_path="root.mes")
    with pytest.raises(StorageUnavailableError):
        await store.query("AS01", "E1", "m1", RANGE)
    with pytest.raises(StorageUnavailableError):
        await store.ping()
    assert pool.returned == 2
