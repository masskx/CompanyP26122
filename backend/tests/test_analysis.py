"""Daily analysis on the simulated scenario: records, events, batches (idempotency,
revisions, partial / failed, retries, catch-up) and the GET / POST API."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.analysis.analyzer import Params, StationAnalyzer
from app.analysis.batch import Scheduler
from app.analysis.models import BatchStatus
from app.analysis.store import MemoryAnalysisStore
from app.stations.model import Station
from app.timeseries.iotdb import MemoryTimeSeries, TimeSeriesError

from .conftest import asimulate

pytestmark = pytest.mark.anyio
TZ = ZoneInfo("Asia/Shanghai")


def yesterday() -> date:
    return datetime.now(UTC).astimezone(TZ).date() - timedelta(days=1)


async def records_of(r, batch_id):
    items, _ = await r.analysis.store.list_records(batch_id, {}, 200, 0)
    return {(x.station_id, x.device_id): await r.analysis.store.get_record(x.record_id)
            for x in items}


async def test_scenario(make_resources):
    r, _ = make_resources()
    batch = await r.analysis.run_now(yesterday())
    assert batch.status == BatchStatus.SUCCEEDED and batch.errors == []
    recs = await records_of(r, batch.batch_id)
    kinds = {k: [(e.kind, e.level.value, e.source.value) for e in v.events]
             for k, v in recs.items()}
    assert kinds == {
        ("ST01", "D02"): [("dose_h1", "watch", "rule")] * 2,
        ("ST01", "D03"): [("dose_h1", "watch", "rule"), ("dose_h2", "inspect", "rule")],
        ("ST01", "D04"): [("stuck_detector", "insufficient_data", "data_quality")],
        ("ST01", "D05"): [("offline", "insufficient_data", "source_reported"),
                          ("sampling_gap", "insufficient_data", "data_quality")],
        ("ST01", "D06"): [("device_fault", "inspect", "source_reported")],
        ("ST01", "BATTERY"): [("battery_low_warn", "watch", "rule"),
                              ("slow_charging", "watch", "rule")],
        ("ST02", "D03"): [("no_data", "insufficient_data", "data_quality")],
        ("ST02", "D04"): [("comm_interruption", "insufficient_data", "source_reported")],
        ("ST02", "BATTERY"): [("battery_drop_on_ac", "watch", "rule")],
    }  # normal devices, the disabled channel ST01/D07 and unconfigured ST03 have no record
    d02, d03 = recs[("ST01", "D02")], recs[("ST01", "D03")]
    assert d02.level == "watch" and d03.level == "inspect"  # most severe event wins
    assert [e.start.astimezone(TZ).time() for e in d02.events] == [time(2), time(8)]
    assert [e.corroborated_by_source for e in d02.events] == [True, False]  # 08:00 not reported
    e1 = d02.events[0]
    assert e1.code == "E1" and e1.sample_count == 120 and e1.completeness == 1.0
    assert e1.rule["threshold"] == 0.25 and e1.rule["consecutive"] == 3
    assert e1.min > 0.25 and e1.max > e1.mean > e1.min and e1.std > 0
    assert len(e1.values) == 120 and e1.values[0][0] == e1.start
    assert e1.context["ac_power"] == {"status": "normal"}
    assert e1.context["rainfall"]["status"] == "available" and e1.context["rainfall"]["raining"]
    assert set(e1.comparisons) == {"previous_day", "previous_7_days",
                                   "previous_month_same_day", "previous_year_same_day"}
    assert e1.comparisons["previous_year_same_day"].status == "unavailable"
    assert e1.comparisons["previous_year_same_day"].reason
    assert recs[("ST02", "D03")].data_status == "missing"
    st02_rain = recs[("ST02", "BATTERY")].events[0].context["rainfall"]
    assert st02_rain == {"status": "unavailable", "reason": "该站无雨量计"}
    battery = recs[("ST01", "BATTERY")]
    assert battery.equipment_type == "backup_power"
    assert battery.events[1].context["charge_minutes"] > 180  # 2.5 x 120 expected
    assert battery.day_stats["ac_outages"][0]["minutes"] == pytest.approx(149.5, abs=1)
    # configuration gap recorded once in the batch, not as device records
    gaps = {(g["station_id"], g["scope"]) for g in batch.config_gaps}
    assert {("ST03", "dose_thresholds"), ("ST03", "battery_thresholds")} <= gaps
    assert batch.counts["records"] == 9 and batch.counts["by_level"] == \
        {"watch": 3, "inspect": 2, "insufficient_data": 4}
    # every record is explained (templates without an LLM) with a computed confidence
    for rec in recs.values():
        ex = rec.explanation
        assert ex.method == "template" and 0 < ex.confidence <= 1
        assert {c for item in ex.evidence for c in item.event_ids} == \
            {e.code for e in rec.events}
    # the snapshot keeps the configuration used
    full = await r.analysis.store.get_batch(batch.batch_id, snapshot=True)
    st01 = next(s for s in full.config_snapshot["stations"] if s["station_id"] == "ST01")
    assert st01["dose_l1"] == 0.25 and st01["calibration_factor"] == 1.0
    assert "simulation" not in st01


async def test_calibration_factor_is_applied(make_resources, stations):
    changed = [s.model_copy(update={"calibration_factor": 0.5}) if s.station_id == "ST01"
               else s for s in stations]
    r, _ = make_resources()
    await r.stations.upsert(changed)
    batch = await r.analysis.run_now(yesterday())
    recs = await records_of(r, batch.batch_id)
    # 0.30-0.35 µGy/h x 0.5 stays below L1: our rule no longer fires for D02, but the device
    # itself still reported H1 at 02:00 -> kept as a source-reported event (08:00 was not)
    d02 = recs[("ST01", "D02")]
    assert [(e.kind, e.source.value) for e in d02.events] == [("reported_h1", "source_reported")]
    d03 = recs[("ST01", "D03")]
    rule_events = [e for e in d03.events if e.source == "rule"]
    assert [e.kind for e in rule_events] == ["dose_h1"]  # 0.65 x 0.5 = 0.33 > L1 only
    assert rule_events[0].rule["calibration_factor"] == 0.5
    assert rule_events[0].corroborated_by_source is False  # the device reported H2 there


async def test_idempotency_and_revisions(make_resources):
    r, _ = make_resources()
    day = yesterday()
    first = await r.analysis.run_now(day)
    again, started = await r.analysis.request(day)
    assert not started and again.batch_id == first.batch_id
    forced = await r.analysis.run_now(day, force=True)
    assert forced.revision == 2 and forced.is_current
    assert not (await r.analysis.store.get_batch(first.batch_id)).is_current
    current = await r.analysis.store.current_batch(r.analysis.version, day)
    assert current.batch_id == forced.batch_id
    with pytest.raises(ValueError):
        await r.analysis.request(r.analysis.today())


class FailingStore(MemoryTimeSeries):
    def __init__(self, inner, fail_on=lambda site, node: True):
        super().__init__()
        self.data, self.fail_on = inner.data, fail_on

    async def query(self, site, device_id, metric, start, end, limit=None):
        if self.fail_on(site, device_id):
            raise TimeSeriesError("connection refused")
        return await super().query(site, device_id, metric, start, end, limit)


async def test_partial_batch_keeps_other_records(make_resources, store):
    r, _ = make_resources(timeseries=FailingStore(store, lambda s, n: (s, n) == ("ST01", "D03")))
    batch = await r.analysis.run_now(yesterday())
    assert batch.status == BatchStatus.PARTIAL
    assert [(e["station_id"], e["device_id"]) for e in batch.errors] == [("ST01", "D03")]
    recs = await records_of(r, batch.batch_id)
    assert ("ST01", "D02") in recs and ("ST01", "D03") not in recs
    assert batch.is_current  # partial results are visible


async def test_storage_down_fails_without_inventing_missing_data(make_resources, store):
    r, _ = make_resources(timeseries=FailingStore(store))
    batch = await r.analysis.run_now(yesterday())
    assert batch.status == BatchStatus.FAILED
    assert (await records_of(r, batch.batch_id)) == {}
    assert await r.analysis.store.current_batch(r.analysis.version) is None
    # a failed batch is retried in place (same id, attempts + 1)
    r.analysis.analyzer.store = store
    retried = await r.analysis.run_now(yesterday())
    assert retried.batch_id == batch.batch_id and retried.attempts == 2
    assert retried.status == BatchStatus.SUCCEEDED and retried.trigger == "manual"


async def test_scheduler_catch_up_and_retry(make_resources, settings):
    s = settings.model_copy(update={"analysis_catchup_days": 3, "analysis_max_attempts": 2,
                                    "analysis_retry_minutes": 10})
    r, _ = make_resources(settings_=s, timeseries=MemoryTimeSeries())
    sched = Scheduler(r.analysis)
    today = r.analysis.today()
    before_run = datetime.combine(today, time(0, 10), TZ).astimezone(UTC)
    after_run = datetime.combine(today, time(0, 40), TZ).astimezone(UTC)
    assert sched.due_days(before_run) == [today - timedelta(days=3), today - timedelta(days=2)]
    assert sched.due_days(after_run)[-1] == today - timedelta(days=1)
    assert sched.next_run(before_run) == r.analysis.run_time(today)
    started = await sched.check(after_run)
    assert len(started) == 3
    for bid in started:
        b = await r.analysis.wait(bid)
        assert b.status == BatchStatus.SUCCEEDED  # empty store: every device "no data"
    assert await sched.check(after_run) == []  # idempotent
    # interrupted batch (restart) -> failed -> retried after the delay, up to max attempts
    day = today - timedelta(days=1)
    [b] = await r.analysis.store.batches_for(day, r.analysis.version)
    await r.analysis.store.update_batch(b.batch_id, status=BatchStatus.RUNNING)
    await sched.recover()
    failed = await r.analysis.store.get_batch(b.batch_id)
    assert failed.status == BatchStatus.FAILED
    assert await sched.check(failed.finished_at + timedelta(minutes=5)) == []
    assert await sched.check(failed.finished_at + timedelta(minutes=11)) == [b.batch_id]
    retried = await r.analysis.wait(b.batch_id)
    assert retried.attempts == 2 and retried.trigger == "retry"


async def test_comparisons_with_history():
    """A small station with 9 days of history: day-on-day and 7-day comparisons available."""
    station = Station.model_validate({
        "station_id": "SX", "name": "x", "sampling_interval_s": 300, "dose_l1": 0.25,
        "dose_l2": 0.5, "devices": [{"device_id": "D1", "name": "d", "simulation": {
            "baseline": 0.1, "noise": 0.02,
            "events": [{"type": "elevation", "day": -1, "start": "03:00", "minutes": 60,
                        "level": 0.3}]}}]})
    now = datetime(2026, 11, 9, 1, 0, tzinfo=TZ).astimezone(UTC)
    ts = await asimulate([station], 9 * 24 + 2, now)
    start = datetime(2026, 11, 8, tzinfo=TZ).astimezone(UTC)
    res = await StationAnalyzer(ts, Params(), TZ).analyze(station, "b", date(2026, 11, 8), start,
                                                          start + timedelta(days=1))
    [rec] = res.records
    [ev] = rec.events
    c = ev.comparisons
    assert c["previous_day"].status == "available" and c["previous_day"].mean < 0.12
    assert c["previous_day"].delta_mean > 0.15 and c["previous_day"].delta_pct > 100
    assert c["previous_7_days"].status == "available" and c["previous_7_days"].days_available == 7
    assert c["previous_month_same_day"].status == "unavailable"
    assert "无历史数据" in c["previous_month_same_day"].reason


async def test_api(make_client):
    client, _ = await make_client(None)
    r = client.resources
    empty = (await client.get("/analysis/records")).json()
    assert empty == {"batch": None, "total": 0, "items": []}  # GET never starts a run
    assert await r.analysis.store.running_batches() == []
    resp = await client.post("/analysis/batches", json={})
    assert resp.status_code == 202 and resp.json()["started"]
    bid = resp.json()["batch"]["batch_id"]
    assert resp.json()["poll"].endswith(bid)
    await r.analysis.wait(bid)
    batch = (await client.get(f"/analysis/batches/{bid}")).json()
    assert batch["status"] == "succeeded" and batch["config_snapshot"] is None
    snap = (await client.get(f"/analysis/batches/{bid}", params={"include_snapshot": True})).json()
    assert len(snap["config_snapshot"]["stations"]) == 3
    again = await client.post("/analysis/batches", json={})
    assert again.status_code == 200 and not again.json()["started"]
    assert (await client.post("/analysis/batches",
                              json={"date": r.analysis.today().isoformat()})).status_code == 422

    listed = (await client.get("/analysis/records")).json()  # default: latest day
    assert listed["batch"]["batch_id"] == bid and listed["total"] == 9
    levels = [x["level"] for x in listed["items"]]
    assert levels == sorted(levels, key=["inspect", "watch", "insufficient_data"].index)
    assert all(x["events"] is None for x in listed["items"])
    st02 = (await client.get("/analysis/records", params={"station_id": "ST02",
                                                         "equipment_type": "detector"})).json()
    assert {x["device_id"] for x in st02["items"]} == {"D03", "D04"}
    by_date = (await client.get("/analysis/records",
                                params={"date": batch["analysis_date"],
                                        "level": "inspect"})).json()
    assert by_date["total"] == 2
    rid = next(x["record_id"] for x in listed["items"] if x["device_id"] == "D02")
    detail = (await client.get(f"/analysis/records/{rid}")).json()
    assert detail["level_label"] == "需关注" and len(detail["events"]) == 2
    assert len(detail["events"][0]["values"]) == 120
    assert detail["explanation"]["evidence"][0]["source"] == "规则计算"
    slim = (await client.get(f"/analysis/records/{rid}", params={"include_values": False})).json()
    assert slim["events"][0]["values"] is None
    page = (await client.get("/analysis/batches")).json()
    assert page["total"] == 1 and page["items"][0]["is_current"]
    status = (await client.get("/analysis/status")).json()
    assert status["latest_batch"]["batch_id"] == bid
    assert status["yesterday_batch"]["batch_id"] == bid
    assert status["schedule"]["enabled"] is False
    assert (await client.get("/analysis/records/nope")).status_code == 404
    assert (await client.get("/analysis/batches/nope")).status_code == 404


async def test_memory_store_rejects_duplicate_revision():
    from app.analysis.models import Batch

    store = MemoryAnalysisStore()
    now = datetime.now(UTC)
    b = Batch(batch_id="x", analysis_date=date(2026, 11, 8), version="v1", revision=1,
              status=BatchStatus.RUNNING, trigger="manual", attempts=1, timezone="UTC",
              window_start=now, window_end=now, created_at=now)
    await store.create_batch(b)
    with pytest.raises(ValueError):
        await store.create_batch(b.model_copy(update={"batch_id": "y"}))
