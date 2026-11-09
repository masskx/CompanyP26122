"""Real IoTDB / MQTT. Skipped unless IOTDB_TEST_HOST / MQTT_TEST_HOST are set."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.ingestion.parsers import SimulatorFormat
from app.ingestion.pipeline import MqttPublisher, MqttSource, Pipeline
from app.ingestion.simulator import SimulatedSource
from app.stations.model import Station
from app.timeseries.iotdb import IoTDB, Measurement, MemoryTimeSeries

pytestmark = pytest.mark.anyio


@pytest.fixture
async def iotdb():
    if not os.environ.get("IOTDB_TEST_HOST"):
        pytest.skip("IOTDB_TEST_HOST not set")
    s = Settings(_env_file=None, iotdb_host=os.environ["IOTDB_TEST_HOST"],
                 iotdb_root=f"root.it_{uuid.uuid4().hex[:8]}")
    db = IoTDB(s)
    yield db
    await db._run(lambda session: session.execute_non_query_statement(
        f"DELETE DATABASE {s.iotdb_root}"))
    await db.close()


async def test_iotdb_schema_write_query_aggregate(iotdb):
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    assert (await iotdb.ensure_schema([("ST01", "D01", "dose_rate")]))["created"] == 1
    assert (await iotdb.ensure_schema([("ST01", "D01", "dose_rate")]))["existing"] == 1
    await iotdb.write([Measurement("ST01", "D01", "dose_rate",
                                   t0 + timedelta(minutes=i), float(i)) for i in range(30)])
    points = await iotdb.query("ST01", "D01", "dose_rate", t0, t0 + timedelta(hours=1))
    assert [p.value for p in points] == [float(i) for i in range(30)]
    buckets = await iotdb.aggregate("ST01", "D01", "dose_rate", t0,
                                    t0 + timedelta(minutes=30), 600)
    assert [(b.avg, b.min, b.max, b.count) for b in buckets] == \
        [(4.5, 0.0, 9.0, 10), (14.5, 10.0, 19.0, 10), (24.5, 20.0, 29.0, 10)]
    latest = await iotdb.latest("ST01", "D01", "dose_rate")
    assert latest.value == 29.0
    assert await iotdb.query("ST01", "NOPE", "dose_rate", t0, t0 + timedelta(hours=1)) == []
    assert await iotdb.latest("ST01", "NOPE", "dose_rate") is None
    assert await iotdb.delete([("ST01", "D01", "dose_rate")]) == 1
    await iotdb.ensure_schema([("ST01", "_station", "battery_voltage")])
    await iotdb.write([Measurement("ST01", "_station", "battery_voltage", t0, 13.4)])
    assert (await iotdb.latest("ST01", "_station", "battery_voltage")).value == 13.4
    await iotdb.purge()
    assert await iotdb.latest("ST01", "_station", "battery_voltage") is None


async def test_mqtt_round_trip():
    if not os.environ.get("MQTT_TEST_HOST"):
        pytest.skip("MQTT_TEST_HOST not set")
    prefix = f"it/{uuid.uuid4().hex[:8]}"
    s = Settings(_env_file=None, mqtt_host=os.environ["MQTT_TEST_HOST"],
                 mqtt_topics=[f"{prefix}/#"], ingest_topic_prefix=prefix)
    store = MemoryTimeSeries()
    pipe = Pipeline(MqttSource(s), SimulatorFormat(prefix), store, flush_seconds=0.1)
    task = asyncio.create_task(pipe.run())
    await asyncio.sleep(0.5)
    station = Station.model_validate({"station_id": "S", "name": "s", "sampling_interval_s": 60,
                                      "devices": [{"device_id": "D1", "name": "d"}]})
    now = datetime.now(UTC)
    sent = await MqttPublisher(s).run(SimulatedSource([station], backfill_hours=0.5,
                                                      realtime=False, topic_prefix=prefix,
                                                      clock=lambda: now))
    for _ in range(50):
        if pipe.stats.written >= sent * 10:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # 30 packets x (dose, status, comm + 7 station values; no rain gauge)
    assert sent == 30 and pipe.stats.written == 30 * 10 and not pipe.stats.rejected
