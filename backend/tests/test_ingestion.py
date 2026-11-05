"""Message formats, simulator, pipeline behaviour."""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.config import BASE_DIR, Settings
from app.ingestion.parsers import (
    MappingParser,
    RawMessage,
    Rejected,
    SimulatorFormat,
    build_parser,
)
from app.ingestion.pipeline import FileSource, Pipeline
from app.ingestion.simulator import DeviceSimulator
from app.timeseries.devices import Device
from app.timeseries.iotdb import MemoryTimeSeries, TimeSeriesError

MQTT = BASE_DIR / "data" / "mqtt"
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
FMT = SimulatorFormat("sim/radiation")


def msg(topic: str, payload) -> RawMessage:
    return RawMessage(topic, payload if isinstance(payload, bytes) else
                      json.dumps(payload).encode())


def test_simulator_format_round_trip():
    raw = FMT.encode("AS01", "AS01-ISO-RM-01", "dose_rate", NOW, 0.512)
    assert raw.topic == "sim/radiation/AS01/AS01-ISO-RM-01/dose_rate"
    [m] = FMT.parse(raw)
    assert (m.site, m.device_id, m.metric, m.time, m.value) == \
        ("AS01", "AS01-ISO-RM-01", "dose_rate", NOW, 0.512)


@pytest.mark.parametrize("unit, value, expected", [("nSv/h", 85, 0.085), ("µSv/h", 1.5, 1.5),
                                                   ("uSv/h", "2", 2.0), ("mSv/h", 0.002, 2.0)])
def test_units(unit, value, expected):
    [m] = FMT.parse(msg("sim/radiation/S/D/dose_rate", {"ts": 1, "value": value, "unit": unit}))
    assert m.value == pytest.approx(expected)


@pytest.mark.parametrize("topic, payload, reason", [
    ("other/S/D/dose_rate", {"ts": 1, "value": 1, "unit": "uSv/h"}, "bad_topic"),
    ("sim/radiation/S/D/dose_rate", b"{", "bad_payload"),
    ("sim/radiation/S/D/dose_rate", {"ts": 1, "value": "x", "unit": "uSv/h"}, "bad_value"),
    ("sim/radiation/S/D/dose_rate", {"ts": 1, "value": 1, "unit": "rem/h"}, "unknown_unit"),
    ("sim/radiation/S/D/dose_rate", {"ts": "2026-01-01T00:00:00", "value": 1, "unit": "uSv/h"},
     "bad_timestamp"),
    ("sim/radiation/S/D/dose_rate", {"ts": 1, "value": 1, "unit": "uSv/h", "status": "fault"},
     "device_status"),
])
def test_rejections(topic, payload, reason):
    with pytest.raises(Rejected) as exc:
        FMT.parse(msg(topic, payload))
    assert exc.value.reason == reason


def test_mapping_example_format():
    parser = MappingParser.from_file(MQTT / "mapping.example.json")
    out = parser.parse(msg("plant/AS01/radiation/gw-1", {"readings": [
        {"deviceId": "GM-0001", "time": "2026-11-05T10:00:00+08:00", "doseRate": 512,
         "unit": "nSv/h", "state": "NORMAL"},
        {"deviceId": "GM-0002", "time": "2026-11-05T10:00:00+08:00", "doseRate": 1,
         "state": "FAULT"}]}))
    assert [(m.device_id, m.value) for m in out] == [("AS01-ISO-RM-01", pytest.approx(0.512))]
    assert out[0].time == datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    assert parser.partial_rejections == {"device_status": 1}


def test_build_parser_choices():
    s = Settings(_env_file=None)
    assert isinstance(build_parser(s), SimulatorFormat)
    mapped = s.model_copy(update={"ingest_parser": "mapping",
                                  "ingest_mapping_file": MQTT / "mapping.example.json"})
    assert isinstance(build_parser(mapped), MappingParser)
    custom = s.model_copy(update={"ingest_parser": "tests.test_ingestion:CustomParser"})
    assert isinstance(build_parser(custom), CustomParser)


class CustomParser:
    def __init__(self, settings):
        self.settings = settings

    def parse(self, message):
        return []


@pytest.mark.anyio
async def test_replay_captured_file():
    store = MemoryTimeSeries()
    p = Pipeline(FileSource(MQTT / "captured.example.jsonl"),
                 MappingParser.from_file(MQTT / "mapping.example.json"), store)
    await p.run()
    assert p.stats.written == 3
    assert p.stats.rejected == {"device_status": 1, "unmapped_device": 1, "bad_topic": 1}


@pytest.mark.anyio
async def test_pipeline_validation_retry_and_buffer(monkeypatch):
    import app.ingestion.pipeline as pl

    async def instant(_):
        return None

    monkeypatch.setattr(pl.asyncio, "sleep", instant)

    class Flaky(MemoryTimeSeries):
        failures = 2

        async def write(self, batch):
            if self.failures:
                self.failures -= 1
                raise TimeSeriesError("down")
            await super().write(batch)

    msgs = [FMT.encode("S", "D1", "dose_rate", NOW - timedelta(seconds=i), 0.1)
            for i in range(5)]
    msgs.append(FMT.encode("S", "D9", "dose_rate", NOW, 0.1))          # unknown device
    msgs.append(FMT.encode("S", "D1", "dose_rate", NOW, -1.0))         # negative dose rate

    class ListSource:
        async def run(self, emit):
            for m in msgs:
                await emit(m)

    store = Flaky()
    p = Pipeline(ListSource(), FMT, store, known_devices={"D1"}, batch_size=2)
    await p.run()
    assert p.stats.written == 5 and p.stats.write_failures == 2
    assert p.stats.rejected == {"unknown_device": 1, "bad_value": 1}

    p2 = Pipeline(ListSource(), FMT, MemoryTimeSeries(), batch_size=100, max_buffer=3)
    await p2.run()
    assert p2.stats.dropped == 3 and p2.stats.written == 3


def test_simulator_events_and_determinism():
    dev = Device(device_id="D", site="S", name="d", metrics=[{"interval_seconds": 60}],
                 simulation={"baseline": 1.0, "noise": 0.02, "drift": 0, "events": [
                     {"type": "elevation", "start_minutes": -120, "duration_minutes": 30,
                      "factor": 10},
                     {"type": "dropout", "start_minutes": -60, "duration_minutes": 20}]})

    def series(seed):
        sim = DeviceSimulator(dev, random.Random(seed), NOW - timedelta(hours=3), NOW)
        out = {}
        while sim.next_time <= NOW:
            t, v = sim.step()
            out[t] = v
        return out

    s = series(1)
    window = [v for t, v in s.items() if NOW - timedelta(minutes=120) <= t <
              NOW - timedelta(minutes=90)]
    assert all(8 < v < 12 for v in window)
    assert [v for t, v in s.items() if NOW - timedelta(minutes=60) <= t <
            NOW - timedelta(minutes=40)] == [None] * 20
    assert series(1) == s and series(2) != s
