"""Message formats, station simulator, pipeline behaviour."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.config import BASE_DIR, Settings
from app.ingestion.parsers import (
    MappingParser,
    RawMessage,
    Rejected,
    TelemetryV1Format,
    build_parser,
)
from app.ingestion.pipeline import FileSource, Pipeline
from app.ingestion.simulator import SimulatedSource
from app.stations.model import Station
from app.timeseries.iotdb import WATERMARK_SERIES, MemoryTimeSeries, TimeSeriesError

MQTT = BASE_DIR / "data" / "mqtt"
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
FMT = TelemetryV1Format()


def msg(topic: str, payload) -> RawMessage:
    return RawMessage(topic, payload if isinstance(payload, bytes) else
                      json.dumps(payload).encode())


def v1(points, seq=7, site="AS01", equipment="AS01-DET-01", **extra) -> RawMessage:
    body = {"schema_version": "1", "message_id": "m1", "source_sequence": seq,
            "site_id": site, "equipment_id": equipment,
            "event_time": "2026-11-05T11:00:00+08:00", "points": points, **extra}
    return msg(f"maintenance/v1/{site}/{equipment}/telemetry", body)


def test_telemetry_v1_round_trip():
    raw = FMT.encode("AS01", "AS01-DET-01", NOW, {"dose_rate": 0.286, "dose_rate_avg": 0.269,
                                                  "status": "h1", "comm": "ok"}, sequence=42)
    assert raw.topic == "maintenance/v1/AS01/AS01-DET-01/telemetry"
    got = {m.metric: m.value for m in FMT.parse(raw)}
    assert got == {"dose_rate": 0.286, "dose_rate_avg": 0.269, "status": 1.0, "comm": 1.0}
    assert FMT.last_sequence == 42 and {m.time for m in FMT.parse(raw)} == {NOW}
    battery = FMT.parse(FMT.encode("AS06", "AS06-BAT-01", NOW, {
        "battery_voltage": 12.03, "ac_power": "off", "charging": False, "runtime_hours": 18}))
    assert {m.metric: m.value for m in battery} == {"battery_voltage": 12.03, "ac_power": 0.0,
                                                   "charging": 0.0, "runtime_hours": 18.0}


@pytest.mark.parametrize("unit, value, expected", [("nGy/h", 85, 0.085), ("µGy/h", 1.5, 1.5),
                                                   ("uGy/h", "2", 2.0), ("mGy/h", 0.002, 2.0),
                                                   (None, 0.3, 0.3)])
def test_units(unit, value, expected):
    [m] = FMT.parse(v1([{"metric": "dose_rate", "value": value, "unit": unit}]))
    assert m.value == pytest.approx(expected)


def test_single_point_form_and_partial_rejection():
    fmt = TelemetryV1Format()
    [m] = fmt.parse(v1(None, metric="dose_rate", value=0.1, quality="good"))
    assert m.value == 0.1
    out = fmt.parse(v1([{"metric": "dose_rate", "value": 0.1},
                        {"metric": "status", "value": "weird"},
                        {"metric": "comm", "value": "ok", "quality": "bad"}]))
    assert [x.metric for x in out] == ["dose_rate"]
    assert fmt.partial_rejections == {"unknown_status": 1, "bad_quality": 1}


@pytest.mark.parametrize("message, reason", [
    (msg("other/AS01/AS01-DET-01/telemetry", {}), "bad_topic"),
    (msg("maintenance/v1/AS01/AS01-DET-01/status", {}), "bad_topic"),
    (msg("maintenance/v1/AS01/AS01-DET-01/telemetry", b"{"), "bad_payload"),
    (v1([{"metric": "dose_rate", "value": "x"}]), "bad_value"),
    (v1([{"metric": "dose_rate", "value": 1, "unit": "uSv/h"}]), "unknown_unit"),
    (v1([{"metric": "humidity_x", "value": 1}]), "unknown_metric"),
    (v1([{"metric": "dose_rate", "value": 1}], event_time="2026-01-01T00:00:00"),
     "bad_timestamp"),
    (v1([{"metric": "dose_rate", "value": 1}], site_id="AS02"), "topic_mismatch"),
    (v1([{"metric": "dose_rate", "value": 1}], schema_version="2"), "unsupported_schema"),
])
def test_rejections(message, reason):
    fmt = TelemetryV1Format()
    with pytest.raises(Rejected) as exc:
        fmt.parse(message)
    assert exc.value.reason == reason


@pytest.mark.anyio
async def test_watermark_advances_after_writes_including_rejected():
    msgs = [v1([{"metric": "dose_rate", "value": 0.1}], seq=1),
            v1([{"metric": "dose_rate", "value": "bad"}], seq=2)]

    class ListSource:
        async def run(self, emit):
            for m in msgs:
                await emit(m)

    store = MemoryTimeSeries()
    p = Pipeline(ListSource(), TelemetryV1Format(), store)
    await p.run()
    assert p.watermark == 2 and p.stats.written == 1 and p.stats.rejected == {"bad_value": 1}
    assert (await store.latest(*WATERMARK_SERIES)).value == 2.0


def test_mapping_example_format():
    parser = MappingParser.from_file(MQTT / "mapping.example.json")
    out = parser.parse(msg("plant/AS01/gamma/gw-1", {"readings": [
        {"deviceId": "GM-0001", "time": "2026-11-05T10:00:00+08:00", "value": 512,
         "unit": "nGy/h", "state": "NORMAL"},
        {"deviceId": "GM-0002", "time": "2026-11-05T10:00:00+08:00", "value": 1,
         "state": "FAULT"},
        {"deviceId": "GM-0003", "time": "2026-11-05T10:00:00+08:00", "value": 1,
         "state": "???"}]}))
    assert [(m.device_id, m.metric, m.value) for m in out] == [
        ("AS01-DET-01", "status", 0.0), ("AS01-DET-01", "dose_rate", pytest.approx(0.512)),
        ("AS02-DET-01", "status", 3.0), ("AS02-DET-01", "dose_rate", pytest.approx(0.001))]
    assert out[0].time == datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    assert parser.partial_rejections == {"unknown_status": 1}


def test_build_parser_choices():
    s = Settings(_env_file=None)
    assert isinstance(build_parser(s), TelemetryV1Format)
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
    assert store.data[("AS01", "AS01-DET-01", "status")]  # fault is stored, not dropped


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
        return FMT.encode("S", device, NOW - timedelta(seconds=seconds), {"dose_rate": value})

    msgs = [packet("D1", 0.1, i) for i in range(5)]
    msgs.append(packet("D9", 0.1))   # unknown device
    msgs.append(packet("D1", -1.0))  # negative dose rate

    class ListSource:
        async def run(self, emit):
            for m in msgs:
                await emit(m)

    store = Flaky()
    p = Pipeline(ListSource(), FMT, store, known_devices={("S", "D1")}, batch_size=2)
    await p.run()
    assert p.stats.written == 5 and p.stats.write_failures == 2
    assert p.stats.rejected == {"unknown_device": 1, "bad_value": 1}

    p2 = Pipeline(ListSource(), FMT, MemoryTimeSeries(), batch_size=100, max_buffer=3)
    await p2.run()
    assert p2.stats.dropped == 3 and p2.stats.written == 3  # the watermark is not counted


@pytest.mark.anyio
async def test_simulator_scenario_and_determinism():
    station = Station.model_validate({
        "station_id": "S", "name": "s", "sampling_interval_s": 60, "dose_l1": 0.25,
        "dose_l2": 0.5, "has_rain_gauge": True,
        "simulation": {"events": [{"type": "ac_loss", "ago_hours": 3, "minutes": 30},
                                  {"type": "runtime_drop", "ago_hours": 1, "minutes": 60,
                                   "runtime_hours": 18}]},
        "devices": [{"device_id": "S-DET-01", "name": "d", "simulation": {
            "baseline": 0.1, "noise": 0.02, "events": [
                {"type": "elevation", "ago_hours": 2, "minutes": 30, "level": 0.6},
                {"type": "gap", "ago_hours": 1, "minutes": 20}]}}]})
    now = datetime(2026, 11, 4, 19, 0, tzinfo=UTC)

    async def run(seed):
        ts = MemoryTimeSeries()
        src = SimulatedSource([station], backfill_hours=3, realtime=False, seed=seed,
                              clock=lambda: now, first_sequence=100)
        pipe = Pipeline(src, TelemetryV1Format(), ts, batch_size=10_000)
        await pipe.run()
        return {k: v for k, v in ts.data.items() if k[0] != "_sys"}, pipe.watermark

    data, watermark = await run(1)
    assert watermark == 100 + 3 * 181 - 20 - 1  # 3 units x 181 instants, minus the gap
    ms = lambda t: int(t.timestamp() * 1000)  # noqa: E731
    dose = data[("S", "S-DET-01", "dose_rate")]
    status = data[("S", "S-DET-01", "status")]
    elev = range(ms(now) - 2 * 3_600_000, ms(now) - 2 * 3_600_000 + 30 * 60_000, 60_000)
    assert all(0.5 < dose[t] < 0.7 for t in elev) and all(status[t] == 2.0 for t in elev)
    gap = range(ms(now) - 3_600_000, ms(now) - 3_600_000 + 20 * 60_000, 60_000)
    assert not any(t in dose for t in gap)
    ac = data[("S", "S-BAT-01", "ac_power")]
    assert {ac[t] for t in range(ms(now) - 3 * 3_600_000, ms(now) - 3 * 3_600_000 + 30 * 60_000,
                                 60_000)} == {0.0}
    assert data[("S", "S-BAT-01", "runtime_hours")][ms(now) - 60_000] == 18.0
    assert ("S", "S-ENV-01", "rainfall") in data
    assert ("S", "S-DET-01", "dose_rate_avg") in data
    assert (await run(1))[0] == data and (await run(2))[0] != data
