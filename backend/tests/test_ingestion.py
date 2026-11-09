"""Message formats, station simulator, pipeline behaviour."""

import json
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
from app.ingestion.simulator import SimulatedSource
from app.stations.model import Station
from app.timeseries.iotdb import MemoryTimeSeries, TimeSeriesError

MQTT = BASE_DIR / "data" / "mqtt"
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
FMT = SimulatorFormat("sim/stations")


def msg(topic: str, payload) -> RawMessage:
    return RawMessage(topic, payload if isinstance(payload, bytes) else
                      json.dumps(payload).encode())


def test_station_packet_round_trip():
    raw = FMT.encode("ST01", NOW, [{"id": "D01", "dose_rate": 85, "unit": "nGy/h",
                                    "status": "h1", "comm": "ok"},
                                   {"id": "D02", "status": "offline", "comm": "interrupted"}],
                     battery={"voltage": 13.4, "level": 98, "ac_power": "off",
                              "charging": True},
                     shelter={"temperature": 22.5, "humidity": 46, "door": "open"}, rainfall=0.2)
    assert raw.topic == "sim/stations/ST01"
    got = {(m.device_id, m.metric): m.value for m in FMT.parse(raw)}
    assert got == {("D01", "dose_rate"): pytest.approx(0.085), ("D01", "status"): 1.0,
                   ("D01", "comm"): 1.0, ("D02", "status"): 4.0, ("D02", "comm"): 0.0,
                   ("_station", "battery_voltage"): 13.4, ("_station", "battery_level"): 98.0,
                   ("_station", "ac_power"): 0.0, ("_station", "charging"): 1.0,
                   ("_station", "shelter_temp"): 22.5, ("_station", "shelter_humidity"): 46.0,
                   ("_station", "door"): 1.0, ("_station", "rainfall"): 0.2}
    assert {m.time for m in FMT.parse(raw)} == {NOW}


@pytest.mark.parametrize("unit, value, expected", [("nGy/h", 85, 0.085), ("µGy/h", 1.5, 1.5),
                                                   ("uGy/h", "2", 2.0), ("mGy/h", 0.002, 2.0)])
def test_units(unit, value, expected):
    [m] = FMT.parse(msg("sim/stations/S", {"ts": 1, "devices": [
        {"id": "D", "dose_rate": value, "unit": unit}]}))
    assert m.value == pytest.approx(expected)


@pytest.mark.parametrize("topic, payload, reason", [
    ("other/S", {"ts": 1}, "bad_topic"),
    ("sim/stations/S", b"{", "bad_payload"),
    ("sim/stations/S", {"ts": 1, "devices": [{"id": "D", "dose_rate": "x"}]}, "bad_value"),
    ("sim/stations/S", {"ts": 1, "devices": [{"id": "D", "dose_rate": 1, "unit": "uSv/h"}]},
     "unknown_unit"),  # Sv -> Gy needs a plant-confirmed coefficient
    ("sim/stations/S", {"ts": "2026-01-01T00:00:00", "devices": []}, "bad_timestamp"),
    ("sim/stations/S", {"ts": 1, "devices": [{"id": "D", "status": "broken"}]},
     "unknown_status"),
])
def test_rejections(topic, payload, reason):
    with pytest.raises(Rejected) as exc:
        FMT.parse(msg(topic, payload))
    assert exc.value.reason == reason


def test_one_bad_field_keeps_the_rest():
    fmt = SimulatorFormat()
    out = fmt.parse(msg("sim/stations/S", {"ts": 1, "devices": [
        {"id": "D", "dose_rate": 0.1, "status": "weird"}], "battery": {"voltage": 13}}))
    assert {m.metric for m in out} == {"dose_rate", "battery_voltage"}
    assert fmt.partial_rejections == {"unknown_status": 1}


def test_mapping_example_format():
    parser = MappingParser.from_file(MQTT / "mapping.example.json")
    out = parser.parse(msg("plant/ST01/gamma/gw-1", {"readings": [
        {"deviceId": "GM-0001", "time": "2026-11-05T10:00:00+08:00", "value": 512,
         "unit": "nGy/h", "state": "NORMAL"},
        {"deviceId": "GM-0002", "time": "2026-11-05T10:00:00+08:00", "value": 1,
         "state": "FAULT"},
        {"deviceId": "GM-0003", "time": "2026-11-05T10:00:00+08:00", "value": 1,
         "state": "???"}]}))
    assert [(m.device_id, m.metric, m.value) for m in out] == [
        ("D01", "status", 0.0), ("D01", "dose_rate", pytest.approx(0.512)),
        ("D02", "status", 3.0), ("D02", "dose_rate", pytest.approx(0.001))]
    assert out[0].time == datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    assert parser.partial_rejections == {"unknown_status": 1}


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
    assert p.stats.written == 9
    assert p.stats.rejected == {"unmapped_device": 1, "bad_topic": 1, "unknown_status": 1}
    assert store.data[("ST01", "D01", "status")]  # fault is stored, not dropped


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

    def packet(device, value, seconds=0):
        return FMT.encode("S", NOW - timedelta(seconds=seconds),
                          [{"id": device, "dose_rate": value}])

    msgs = [packet("D1", 0.1, i) for i in range(5)]
    msgs.append(packet("D9", 0.1))   # unknown device
    msgs.append(packet("D1", -1.0))  # negative dose rate

    class ListSource:
        async def run(self, emit):
            for m in msgs:
                await emit(m)

    store = Flaky()
    p = Pipeline(ListSource(), FMT, store, known_devices={("S", "D1"), ("S", "_station")},
                 batch_size=2)
    await p.run()
    assert p.stats.written == 5 and p.stats.write_failures == 2
    assert p.stats.rejected == {"unknown_device": 1, "bad_value": 1}

    p2 = Pipeline(ListSource(), FMT, MemoryTimeSeries(), batch_size=100, max_buffer=3)
    await p2.run()
    assert p2.stats.dropped == 3 and p2.stats.written == 3


@pytest.mark.anyio
async def test_simulator_scenario_and_determinism():
    station = Station.model_validate({
        "station_id": "S", "name": "s", "sampling_interval_s": 60, "dose_l1": 0.25,
        "dose_l2": 0.5, "has_rain_gauge": True,
        "simulation": {"events": [{"type": "ac_loss", "day": 0, "start": "00:00",
                                   "minutes": 30}]},
        "devices": [{"device_id": "D", "name": "d", "simulation": {
            "baseline": 0.1, "noise": 0.02, "events": [
                {"type": "elevation", "day": 0, "start": "01:00", "minutes": 30, "level": 0.6},
                {"type": "gap", "day": 0, "start": "02:00", "minutes": 20}]}}]})
    now = datetime(2026, 11, 4, 19, 0, tzinfo=UTC)  # 2026-11-05 03:00 Asia/Shanghai

    async def run(seed):
        ts = MemoryTimeSeries()
        src = SimulatedSource([station], backfill_hours=3, realtime=False, seed=seed,
                              clock=lambda: now)
        await Pipeline(src, SimulatorFormat(), ts, batch_size=10_000).run()
        return ts.data

    data = await run(1)
    dose = data[("S", "D", "dose_rate")]
    status = data[("S", "D", "status")]
    base = int(datetime(2026, 11, 4, 17, 0, tzinfo=UTC).timestamp() * 1000)  # 01:00 local
    hour = 3600_000
    assert all(0.5 < dose[t] < 0.7 for t in range(base, base + hour // 2, 60_000))
    assert all(status[t] == 2.0 for t in range(base, base + hour // 2, 60_000))  # H2 reported
    assert not any(base + hour <= t < base + hour + 20 * 60_000 for t in dose)  # gap
    ac = data[("S", "_station", "ac_power")]
    assert {ac[t] for t in range(base - hour, base - hour // 2, 60_000)} == {0.0}
    assert ("S", "_station", "rainfall") in data
    assert await run(1) == data and await run(2) != data
