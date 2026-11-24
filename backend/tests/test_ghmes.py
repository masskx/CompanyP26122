"""ghmes/signal snapshots (the format sent by the MQTT administrator) -> IoTDB measurements."""

import json
from datetime import UTC, datetime

import pytest

from app.config import BASE_DIR
from app.ingestion.ghmes import GhmesFormat, GhmesProfile
from app.ingestion.parsers import RawMessage, Rejected, TelemetryV1Format
from app.ingestion.pipeline import FileSource, Pipeline
from app.ingestion.simulator import SimulatedSource
from app.maintenance.schemas import AnalysisRequest, watermark_value
from app.stations.model import STATUS_CODES, DeviceStatus
from app.stations.store import known_equipment
from app.timeseries.iotdb import MemoryTimeSeries

pytestmark = pytest.mark.anyio
MQTT = BASE_DIR / "data" / "mqtt"
EXAMPLE = MQTT / "ghmes.example.jsonl"
OFFLINE = float(STATUS_CODES[DeviceStatus.OFFLINE])


def fmt() -> GhmesFormat:
    return GhmesFormat.from_file(MQTT / "ghmes.json")


def captured(line: int) -> RawMessage:
    rows = [json.loads(x) for x in EXAMPLE.read_text(encoding="utf-8").splitlines()
            if x.strip() and not x.startswith("#")]
    return RawMessage(rows[line]["topic"], json.dumps(rows[line]["payload"]).encode())


def by_key(out) -> dict:
    return {(m.site, m.device_id, m.metric): m.value for m in out}


def test_administrator_sample_all_comm_faults():
    """Every channel reports 通讯故障 with placeholder zeros: no zero is stored as a reading."""
    f = fmt()
    out = f.parse(captured(0))
    got = by_key(out)
    assert got == {("AS01", "AS01-ENV-01", "station_status"): OFFLINE,
                   ("AS01", "AS01-DET-01", "status"): OFFLINE,
                   ("AS01", "AS01-DET-01", "comm"): 0.0,
                   ("M01", "M01-VEH-01", "comm"): 0.0,
                   ("MET01", "MET01-TWR-01", "comm"): 0.0}
    # dataTime has no zone: read in Asia/Shanghai; the watermark is that time in epoch ms
    assert out[0].time == datetime(2026, 11, 23, 12, 16, 16, tzinfo=UTC)
    assert f.last_sequence == int(out[0].time.timestamp() * 1000)
    assert not f.partial_rejections


def test_normal_snapshot():
    out = by_key(fmt().parse(captured(1)))
    det, bat, env = ("AS01", "AS01-DET-01"), ("AS01", "AS01-BAT-01"), ("AS01", "AS01-ENV-01")
    assert out[(*det, "dose_rate")] == pytest.approx(0.086)          # μGy/h as reported
    assert out[(*det, "dose_rate_std")] == pytest.approx(0.004)
    assert out[(*det, "status")] == 0.0 and out[(*det, "comm")] == 1.0
    assert out[(*bat, "power_status")] == 0.0 and out[(*bat, "ac_power")] == 1.0  # code 0 = mains
    assert out[(*env, "station_status")] == 0.0 and out[(*env, "carbon_sampler")] == 0.0
    assert out[("M01", "M01-VEH-01", "gamma_dose_rate")] == pytest.approx(0.11)  # μSv/h as is
    assert out[("M01", "M01-VEH-01", "lat_wgs84")] == pytest.approx(37.1)
    assert out[("MET01", "MET01-TWR-01", "wind_speed_10")] > 0
    assert out[("MET01", "MET01-TWR-01", "comm")] == 1.0
    # everything maps onto equipment of the station directory
    known = known_equipment(BASE_DIR / "data" / "stations.json")
    assert {(s, e) for s, e, _ in out} <= known


def channel(field, value, unit="", comm="通讯正常"):
    return {"fieldName": field, "value": value, "unit": unit, "commStatus": comm}


def signal(name="AS02-综合楼", channels=(), code="sig-002", biz="固定子站",
           time="2026-11-23 20:16:16"):
    return {"signalCode": code, "signalName": name, "bizSystem": biz, "dataTime": time,
            "channels": list(channels)}


def message(*signals, topic="ghmes/signal") -> RawMessage:
    return RawMessage(topic, json.dumps(list(signals), ensure_ascii=False).encode())


def test_partial_problems_are_counted_not_fatal():
    f = fmt()
    out = f.parse(message(
        signal(channels=[channel("gamma_instant", 86, "nGy/h"), channel("gamma_status", 7),
                         channel("mystery", 1), channel("gamma_avg", None),
                         channel("gamma_std", 1, "μSv/h"),
                         channel("total_status", 1, comm="通讯不稳")]),
        signal("潮汐站", code="sig-099"),                      # no station id in the name
        signal("X", biz="废液采样", code="sig-050")))           # unknown object
    got = by_key(out)
    assert got[("AS02", "AS02-DET-01", "dose_rate")] == pytest.approx(0.086)  # nGy/h -> μGy/h
    assert got[("AS02", "AS02-ENV-01", "station_status")] == 1.0  # unknown comm text: kept
    assert ("AS02", "AS02-DET-01", "status") not in got and \
        ("AS02", "AS02-DET-01", "dose_rate_std") not in got
    assert f.partial_rejections == {"unknown_code": 1, "unmapped_field": 1, "missing_value": 1,
                                    "unknown_unit": 1, "unknown_comm_status": 1,
                                    "unmapped_signal": 2}
    with pytest.raises(Rejected, match="bad_topic"):
        f.parse(message(signal(), topic="other/topic"))
    with pytest.raises(Rejected, match="bad_timestamp"):
        f.parse(message(signal(channels=[channel("gamma_instant", 1)], time="yesterday")))


def test_profile_overrides():
    profile = GhmesProfile.model_validate({
        **json.loads((MQTT / "ghmes.json").read_text(encoding="utf-8")),
        "station_map": {"sig-777": "BS11"}, "ignore_fields": ["mystery"]})
    f = GhmesFormat(profile)
    out = by_key(f.parse(message(signal("whatever", code="sig-777", time="2026-11-23T12:00:00Z",
                                        channels=[channel("gamma_instant", 0.1),
                                                  channel("mystery", 2)]))))
    assert out == {("BS11", "BS11-DET-01", "dose_rate"): 0.1, ("BS11", "BS11-DET-01", "comm"): 1}
    assert not f.partial_rejections
    with pytest.raises(ValueError, match="belongs to"):
        GhmesProfile.model_validate({"channels": {"x": [{"unit": "detector",
                                                         "metric": "battery_voltage"}]}})


async def test_pipeline_replays_capture_and_tracks_time_watermark():
    ts = MemoryTimeSeries()
    pipe = Pipeline(FileSource(EXAMPLE), fmt(), ts,
                    known_devices=known_equipment(BASE_DIR / "data" / "stations.json"))
    await pipe.run()
    assert pipe.stats.rejected == {} and pipe.stats.written > 40
    second = datetime(2026, 11, 23, 12, 17, 16, tzinfo=UTC)
    assert pipe.watermark == int(second.timestamp() * 1000)
    # replaying older data later never moves the stored watermark backwards
    later = int(second.timestamp() * 1000) + 3_600_000
    replay = Pipeline(FileSource(EXAMPLE), fmt(), ts, watermark=later)
    await replay.run()
    assert replay.watermark == later and replay.stats.written == pipe.stats.written
    assert (await ts.latest("_sys", "ingest", "watermark")).value == pipe.watermark


async def test_simulator_round_trip_matches_telemetry_v1(stations):
    """The simulator publishes ghmes snapshots; read back, they give the same readings as the
    telemetry_v1 path (so the demo scenario survives the real format)."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    picked = [s for s in stations if s.station_id in ("AS03", "AS06", "BS11", "AS08")]

    async def run(make) -> MemoryTimeSeries:
        ts = MemoryTimeSeries()
        src = SimulatedSource(picked, backfill_hours=3, realtime=False, seed=1,
                              clock=lambda: now, fmt=make())
        await Pipeline(src, make(), ts, batch_size=50_000).run()
        return ts

    ghmes, v1 = await run(fmt), await run(TelemetryV1Format)
    for metric in ("dose_rate", "dose_rate_avg", "battery_voltage", "ac_power", "charging",
                   "runtime_hours", "rainfall", "status", "station_status"):
        keys = [k for k in v1.data if k[2] == metric]
        assert keys, metric
        for k in keys:
            assert ghmes.data.get(k) == pytest.approx(v1.data[k]), k


def test_watermark_accepts_epoch_ms_or_iso_time():
    assert watermark_value("18427") == 18427
    assert watermark_value("2026-11-23T20:16:16+08:00") == 1795436176000
    base = {"analysis_id": "a", "site_id": "AS01", "analysis_type": "detector",
            "observation_window": {"start": "2026-11-23T00:00:00+08:00",
                                   "end": "2026-11-24T00:00:00+08:00"}}
    assert AnalysisRequest.model_validate({**base, "mqtt_watermark": "2026-11-23T20:16:16Z"})
    for bad in ("abc", "2026-11-23T20:16:16"):  # no time zone
        with pytest.raises(ValueError):
            AnalysisRequest.model_validate({**base, "mqtt_watermark": bad})


async def test_auxiliary_endpoints(make_client):
    ts = MemoryTimeSeries()
    await Pipeline(FileSource(EXAMPLE), fmt(), ts).run()
    client, _ = await make_client(None, timeseries=ts)
    sources = (await client.get("/auxiliary")).json()
    assert [a["source_id"] for a in sources] == ["M01", "MET01"]
    m01 = (await client.get("/auxiliary/M01")).json()
    assert m01["latest"]["comm"]["value"] == "ok"
    dose = m01["latest"]["channels"]["gamma_dose_rate"]
    assert dose["value"] == pytest.approx(0.11) and dose["unit"] == "μSv/h"
    t = (await client.get("/auxiliary/MET01/telemetry", params={
        "field": "rainfall", "start": "2026-11-23T00:00:00+08:00",
        "end": "2026-11-24T00:00:00+08:00"})).json()
    assert t["label"] == "降雨量" and len(t["points"]) == 1
    assert (await client.get("/auxiliary/NOPE")).status_code == 404
    assert (await client.get("/auxiliary/M01/telemetry",
                             params={"field": "nope"})).status_code == 404
    st = (await client.get("/stations/AS01")).json()["latest"]
    assert st["environment"]["station_status"]["value"] == "normal"
    assert st["backup_power"]["power_status"] == {"time": st["backup_power"]["power_status"]
                                                  ["time"], "value": 0, "code": True}
    h = (await client.get("/health")).json()["checks"]
    assert h["stations"]["auxiliary_sources"] == 2 and h["mqtt_watermark"]["parser"] == "ghmes"
