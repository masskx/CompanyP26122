"""Integration tests against real services. Skipped unless the services are available:

    IOTDB_TEST_HOST=127.0.0.1 [IOTDB_TEST_PORT=6667]  pytest tests/test_integration.py
    MQTT_TEST_HOST=127.0.0.1  [MQTT_TEST_PORT=1883]

Each run writes under a unique IoTDB database (root.it_<random>) and removes it afterwards.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from industrial_ai.collector.models import Measurement
from industrial_ai.collector.parser import ProvisionalJsonCodec
from industrial_ai.collector.pipeline import IngestionPipeline
from industrial_ai.collector.simulation import DeviceProfile, ScriptedEvent
from industrial_ai.collector.sources.mqtt import MqttConfig, MqttPublisher, MqttSource
from industrial_ai.collector.sources.simulator import SimulatedSource
from industrial_ai.collector.validator import MeasurementValidator
from industrial_ai.collector.writer import InMemoryWriter, IoTDBWriter
from industrial_ai.core.config import Settings
from industrial_ai.schemas.common import TimeRange
from industrial_ai.storage.iotdb_client import IoTDBTimeSeriesStore, pool_factory_from_settings
from industrial_ai.storage.memory import InMemoryTimeSeriesStore

pytestmark = pytest.mark.anyio

IOTDB_HOST = os.environ.get("IOTDB_TEST_HOST")
MQTT_HOST = os.environ.get("MQTT_TEST_HOST")
needs_iotdb = pytest.mark.skipif(not IOTDB_HOST, reason="IOTDB_TEST_HOST not set")
needs_mqtt = pytest.mark.skipif(not MQTT_HOST, reason="MQTT_TEST_HOST not set")


@pytest.fixture
def iotdb_settings():
    root = f"root.it_{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, iotdb_host=IOTDB_HOST or "127.0.0.1",
                        iotdb_port=int(os.environ.get("IOTDB_TEST_PORT", "6667")),
                        iotdb_user=os.environ.get("IOTDB_TEST_USER", "root"),
                        iotdb_password=SecretStr(os.environ.get("IOTDB_TEST_PASSWORD", "root")),
                        iotdb_root_path=root)
    yield settings
    pool = pool_factory_from_settings(settings)()
    session = pool.get_session()
    try:
        session.execute_non_query_statement(f"DELETE DATABASE {root}")
    finally:
        pool.put_back(session)
        pool.close()


@needs_iotdb
async def test_iotdb_write_then_read(iotdb_settings):
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    batch = [Measurement("AS01", "AS01-ISO-RM-01", "dose_rate", t0 + timedelta(seconds=30 * i),
                         0.5 + i / 100) for i in range(10)]
    batch.append(Measurement("AS01", "AS01-ENV-01", "dose_rate", t0, 0.085))

    writer = IoTDBWriter(pool_factory_from_settings(iotdb_settings),
                         root_path=iotdb_settings.iotdb_root_path)
    await writer.write(batch)
    await writer.close()

    store = IoTDBTimeSeriesStore.from_settings(iotdb_settings)
    await store.ping()
    window = TimeRange(start=t0 - timedelta(minutes=1), end=t0 + timedelta(minutes=10))
    points = await store.query("AS01", "AS01-ISO-RM-01", "dose_rate", window)
    assert [p.timestamp for p in points] == [m.timestamp for m in batch[:10]]
    assert [p.value for p in points] == pytest.approx([m.value for m in batch[:10]])
    assert len(await store.query("AS01", "AS01-ENV-01", "dose_rate", window)) == 1
    # A series that was never written reads as empty, not as an error.
    assert await store.query("AS01", "NOPE", "dose_rate", window) == []
    await store.close()


@needs_iotdb
async def test_simulator_pipeline_into_iotdb(iotdb_settings):
    now = datetime.now(UTC)
    device = DeviceProfile(site_id="AS01", equipment_id="AS01-ISO-RM-02", baseline=1.2,
                           interval_seconds=30, events=[ScriptedEvent(
                               type="elevation", start_offset_minutes=-60, duration_minutes=20,
                               factor=15)])
    codec = ProvisionalJsonCodec("sim/radiation")
    source = SimulatedSource([device], codec, backfill=timedelta(hours=2), realtime=False,
                             seed=1, clock=lambda: now)
    writer = IoTDBWriter(pool_factory_from_settings(iotdb_settings),
                         root_path=iotdb_settings.iotdb_root_path)
    pipeline = IngestionPipeline(source, codec, MeasurementValidator(), writer, batch_size=100)
    await pipeline.run()
    await pipeline.close()
    assert pipeline.stats.written == 240

    store = IoTDBTimeSeriesStore.from_settings(iotdb_settings)
    points = await store.query("AS01", "AS01-ISO-RM-02", "dose_rate",
                               TimeRange(start=now - timedelta(hours=3), end=now))
    await store.close()
    assert len(points) == 240
    assert sum(p.value > 12 for p in points) == 40  # the 20-minute elevation


@needs_mqtt
async def test_mqtt_publish_and_subscribe():
    prefix = f"it/{uuid.uuid4().hex[:8]}"
    config = MqttConfig(host=MQTT_HOST or "127.0.0.1",
                        port=int(os.environ.get("MQTT_TEST_PORT", "1883")),
                        topics=(f"{prefix}/#",), qos=1)
    codec = ProvisionalJsonCodec(prefix)
    store = InMemoryTimeSeriesStore()
    pipeline = IngestionPipeline(MqttSource(config), codec, MeasurementValidator(),
                                 InMemoryWriter(store), flush_seconds=0.1)
    task = asyncio.create_task(pipeline.run())
    await asyncio.sleep(0.5)  # let the subscription become active

    now = datetime.now(UTC)
    device = DeviceProfile(site_id="AS01", equipment_id="AS01-ENV-01", baseline=0.085,
                           interval_seconds=60)
    sim = SimulatedSource([device], codec, backfill=timedelta(minutes=30), realtime=False,
                          seed=3, clock=lambda: now)
    async with MqttPublisher(config) as publisher:
        await sim.run(publisher.publish)

    for _ in range(50):
        if pipeline.stats.written >= 30:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert pipeline.stats.written == 30 and not pipeline.stats.rejected
    points = await store.query("AS01", "AS01-ENV-01", "dose_rate",
                               TimeRange(start=now - timedelta(hours=1), end=now))
    assert len(points) == 30 and all(0.05 < p.value < 0.15 for p in points)
