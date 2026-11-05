import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from industrial_ai.api.deps import build_container
from industrial_ai.api.main import create_app
from industrial_ai.collector.models import Measurement, RawMessage, RejectedMessage
from industrial_ai.collector.parser import ProvisionalJsonCodec, build_parser
from industrial_ai.collector.pipeline import IngestionPipeline
from industrial_ai.collector.simulation import (
    DeviceProfile,
    DeviceSimulator,
    ScriptedEvent,
    load_scenario,
)
from industrial_ai.collector.sources.simulator import SimulatedSource
from industrial_ai.collector.validator import MeasurementValidator
from industrial_ai.collector.writer import InMemoryWriter
from industrial_ai.schemas.common import TimeRange
from industrial_ai.storage.base import StorageUnavailableError
from industrial_ai.storage.catalog import StaticEquipmentCatalog
from industrial_ai.storage.memory import InMemoryTimeSeriesStore

from .conftest import AUTH

SCENARIO = Path(__file__).parent.parent / "examples" / "simulation" / "radiation_scenario.json"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
CODEC = ProvisionalJsonCodec("sim/radiation")


def raw(topic: str, payload: dict | bytes) -> RawMessage:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return RawMessage(topic=topic, payload=body, received_at=NOW)


def reason(fn) -> str:
    with pytest.raises(RejectedMessage) as exc:
        fn()
    return exc.value.reason


# ---- parser ------------------------------------------------------------------------------

def test_codec_round_trip():
    topic, payload = CODEC.encode("AS01", "AS01-ISO-RM-01", "dose_rate", NOW, 0.512, "uSv/h")
    assert topic == "sim/radiation/AS01/AS01-ISO-RM-01/dose_rate"
    [m] = CODEC.parse(RawMessage(topic, payload, NOW))
    assert m == Measurement("AS01", "AS01-ISO-RM-01", "dose_rate", NOW, 0.512)


@pytest.mark.parametrize("unit, value, expected", [
    ("nSv/h", 85.0, 0.085), ("uSv/h", 1.5, 1.5), ("µSv/h", 1.5, 1.5), ("μSv/h", 1.5, 1.5),
    ("mSv/h", 0.002, 2.0),
])
def test_units_normalised_to_usv_per_hour(unit, value, expected):
    [m] = CODEC.parse(raw("sim/radiation/S/E/dose_rate", {"ts": 1_700_000_000_000,
                                                            "value": value, "unit": unit}))
    assert m.value == pytest.approx(expected)


def test_timestamp_formats():
    for ts in (1_700_000_000_000, 1_700_000_000, "2023-11-14T22:13:20Z",
               "2023-11-15T06:13:20+08:00"):
        [m] = CODEC.parse(raw("sim/radiation/S/E/dose_rate",
                              {"ts": ts, "value": 1, "unit": "uSv/h"}))
        assert m.timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


@pytest.mark.parametrize("topic, payload, expected", [
    ("other/S/E/dose_rate", {"ts": 1, "value": 1, "unit": "uSv/h"}, "bad_topic"),
    ("sim/radiation/S/E", {"ts": 1, "value": 1, "unit": "uSv/h"}, "bad_topic"),
    ("sim/radiation/S/E/dose_rate", b"\xff\xfe", "bad_payload"),
    ("sim/radiation/S/E/dose_rate", [1, 2], "bad_payload"),
    ("sim/radiation/S/E/dose_rate", {"ts": 1, "value": "1", "unit": "uSv/h"}, "bad_value"),
    ("sim/radiation/S/E/dose_rate", {"ts": 1, "value": True, "unit": "uSv/h"}, "bad_value"),
    ("sim/radiation/S/E/dose_rate", {"ts": 1, "value": 1, "unit": "rem/h"}, "unknown_unit"),
    ("sim/radiation/S/E/dose_rate", {"ts": 1, "value": 1}, "missing_unit"),
    ("sim/radiation/S/E/dose_rate", {"ts": "yesterday", "value": 1, "unit": "uSv/h"},
     "bad_timestamp"),
    ("sim/radiation/S/E/dose_rate", {"ts": "2026-01-01T00:00:00", "value": 1, "unit": "uSv/h"},
     "bad_timestamp"),
    ("sim/radiation/S/E/dose_rate", {"ts": 1, "value": 1, "unit": "uSv/h", "status": "fault"},
     "device_status"),
])
def test_parser_rejections(topic, payload, expected):
    assert reason(lambda: CODEC.parse(raw(topic, payload))) == expected


def test_unknown_parser_name():
    with pytest.raises(ValueError, match="provisional_json"):
        build_parser("real_format_v1", "x")


# ---- validator ---------------------------------------------------------------------------

def test_validator():
    v = MeasurementValidator(known_equipment={("S", "E")}, clock=lambda: NOW,
                             max_age=timedelta(days=1))
    ok = Measurement("S", "E", "dose_rate", NOW, 0.1)
    v.check(ok)
    assert reason(lambda: v.check(Measurement("S", "X", "dose_rate", NOW, 0.1))) \
        == "unknown_equipment"
    assert reason(lambda: v.check(Measurement("S", "E", "dose_rate", NOW, -0.1))) == "bad_value"
    assert reason(lambda: v.check(Measurement("S", "E", "dose_rate", NOW, float("nan")))) \
        == "bad_value"
    assert reason(lambda: v.check(Measurement("S", "E", "dose_rate",
                                              NOW + timedelta(hours=1), 0.1))) \
        == "future_timestamp"
    assert reason(lambda: v.check(Measurement("S", "E", "dose_rate",
                                              NOW - timedelta(days=2), 0.1))) \
        == "stale_timestamp"
    assert reason(lambda: v.check(Measurement("S", "E`x", "dose_rate", NOW, 0.1))) \
        == "bad_identifier"


# ---- simulation model --------------------------------------------------------------------

def run_device(profile: DeviceProfile, hours: float, seed: int = 1):
    sim = DeviceSimulator(profile, random.Random(seed), start=NOW - timedelta(hours=hours),
                          anchor=NOW)
    out = []
    while sim.next_time <= NOW:
        out.append(sim.step())
    return out


def test_background_statistics_are_plausible():
    profile = DeviceProfile(site_id="S", equipment_id="E", baseline=0.5, interval_seconds=30,
                            counting_noise=0.05)
    values = [v for _, v in run_device(profile, 24)]
    assert len(values) == 24 * 120 + 1
    mean = sum(values) / len(values)
    assert 0.45 < mean < 0.55 and min(values) >= 0
    rel_sd = (sum((x - mean) ** 2 for x in values) / len(values)) ** 0.5 / mean
    assert 0.03 < rel_sd < 0.1


def test_scripted_events():
    profile = DeviceProfile(site_id="S", equipment_id="E", baseline=1.0, interval_seconds=60,
                            counting_noise=0.02, drift_sigma_per_day=0, events=[
        ScriptedEvent(type="elevation", start_offset_minutes=-120, duration_minutes=30,
                      factor=10),
        ScriptedEvent(type="dropout", start_offset_minutes=-60, duration_minutes=20),
        ScriptedEvent(type="stuck", start_offset_minutes=-30, duration_minutes=10),
    ])
    series = dict(run_device(profile, 3))

    def window(start_min, end_min):
        return [series[t] for t in series
                if NOW + timedelta(minutes=start_min) <= t < NOW + timedelta(minutes=end_min)]

    assert all(8 < v < 12 for v in window(-120, -90))          # elevated plateau
    assert all(0.9 < v < 1.1 for v in window(-89, -61))         # back to background
    assert window(-60, -40) == [None] * 20                     # dropout: no readings
    stuck = window(-30, -20)
    assert len(set(stuck)) == 1                                # frozen value


def test_simulation_is_deterministic_per_seed():
    profile = DeviceProfile(site_id="S", equipment_id="E", baseline=0.3, interval_seconds=60,
                            random_events={"spike_per_day": 20, "washout_per_day": 5})
    assert run_device(profile, 6, seed=7) == run_device(profile, 6, seed=7)
    assert run_device(profile, 6, seed=7) != run_device(profile, 6, seed=8)


# ---- pipeline ----------------------------------------------------------------------------

class ListSource:
    def __init__(self, messages):
        self.messages = messages

    async def run(self, emit):
        for m in self.messages:
            await emit(m)


class FlakyWriter:
    def __init__(self, failures: int):
        self.failures, self.batches = failures, []

    async def write(self, batch):
        if self.failures:
            self.failures -= 1
            raise StorageUnavailableError("down")
        self.batches.append(list(batch))

    async def close(self):
        pass


@pytest.mark.anyio
async def test_pipeline_counts_rejections_and_retries_writes(monkeypatch):
    import industrial_ai.collector.pipeline as pipeline_mod

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline_mod.asyncio, "sleep", no_sleep)
    good = [raw("sim/radiation/S/E/dose_rate",
                {"ts": int((NOW - timedelta(seconds=i)).timestamp() * 1000), "value": 0.1,
                 "unit": "uSv/h"}) for i in range(5)]
    bad = [raw("bad/topic", {}), raw("sim/radiation/S/E/dose_rate", b"{")]
    writer = FlakyWriter(failures=2)
    pipeline = IngestionPipeline(ListSource([*good, *bad]), CODEC,
                                 MeasurementValidator(clock=lambda: NOW), writer, batch_size=2)
    await pipeline.run()
    assert sum(len(b) for b in writer.batches) == 5
    s = pipeline.stats
    assert (s.received, s.written, s.write_failures) == (7, 5, 2)
    assert s.rejected == {"bad_topic": 1, "bad_payload": 1}


@pytest.mark.anyio
async def test_pipeline_bounds_buffer_during_outage(monkeypatch):
    msgs = [raw("sim/radiation/S/E/dose_rate",
                {"ts": int(NOW.timestamp() * 1000) - i, "value": 1, "unit": "uSv/h"})
            for i in range(10)]
    writer = FlakyWriter(failures=0)
    pipeline = IngestionPipeline(ListSource(msgs), CODEC, MeasurementValidator(clock=lambda: NOW),
                                 writer, batch_size=100, max_buffer=4)
    await pipeline.run()
    assert pipeline.stats.dropped == 6 and pipeline.stats.written == 4


# ---- scenario end to end: simulator -> pipeline -> store -> API --------------------------

def test_shipped_scenario_is_valid():
    scenario = load_scenario(SCENARIO)
    assert {d.equipment_id for d in scenario.simulation.devices} \
        == {e.equipment_id for e in scenario.equipment}
    assert StaticEquipmentCatalog.from_file(SCENARIO).keys()


@pytest.mark.anyio
async def test_simulated_data_drives_health_levels(settings):
    scenario = load_scenario(SCENARIO)
    store = InMemoryTimeSeriesStore()
    catalog = StaticEquipmentCatalog.from_file(SCENARIO)
    now = datetime.now(UTC)
    source = SimulatedSource(scenario.simulation.devices, CODEC, backfill=timedelta(hours=26),
                             realtime=False, seed=42, clock=lambda: now)
    validator = MeasurementValidator(known_equipment=catalog.keys())
    pipeline = IngestionPipeline(source, CODEC, validator, InMemoryWriter(store), batch_size=1000)
    await pipeline.run()
    # 5 devices @30 s + 2 @60 s over 26 h = 18,720 readings, minus scripted/random dropouts.
    assert pipeline.stats.rejected == {} and 17_000 < pipeline.stats.written < 18_720

    points = await store.query("AS01", "AS01-ISO-RM-02", "dose_rate",
                               TimeRange(start=now - timedelta(hours=1), end=now))
    assert len(points) == 120

    container = build_container(settings, catalog=catalog, timeseries=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(container=container)),
                                 base_url="http://t") as c:
        resp = await c.get("/internal/v1/equipment/health", params={"site_id": "AS01"},
                           headers=AUTH)
    await container.aclose()
    levels = {i["equipment_id"]: i["health_level"] for i in resp.json()["items"]}
    assert levels == {
        "AS01-ISO-RM-01": "normal",
        "AS01-ISO-RM-02": "inspect",
        "AS01-ISO-RM-03": "insufficient_data",
        "AS01-ISO-RM-04": "watch",
        "AS01-ISO-RM-05": "normal",
        "AS01-ENV-01": "normal",
        "AS01-ENV-02": "normal",
    }
