"""Analysis + station store contract: memory always, openGauss when OPENGAUSS_TEST_DSN points
at a database the test may migrate (tables are created by the migrations, rows are removed)."""

import os
from datetime import UTC, date, datetime, timedelta

import pytest

from app.analysis.models import (
    Batch,
    BatchStatus,
    Category,
    Comparison,
    EquipmentType,
    Event,
    Explanation,
    Level,
    Record,
    Source,
)
from app.analysis.store import MemoryAnalysisStore
from app.stations.store import MemoryStationStore

pytestmark = pytest.mark.anyio
DAY = date(2001, 1, 2)  # far from real data
NOW = datetime(2001, 1, 3, 1, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "opengauss"])
async def stores(request, stations):
    if request.param == "memory":
        yield MemoryAnalysisStore(), MemoryStationStore([])
        return
    dsn = os.environ.get("OPENGAUSS_TEST_DSN")
    if not dsn:
        pytest.skip("OPENGAUSS_TEST_DSN not set")
    import psycopg

    from app.analysis.store import PgAnalysisStore
    from app.db.migrate import migrate
    from app.db.pool import PgPool
    from app.stations.store import PgStationStore

    await migrate(dsn)

    def cleanup():
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("DELETE FROM analysis_event WHERE record_id IN (SELECT record_id FROM "
                      "analysis_record WHERE analysis_date = %s)", (DAY,))
            c.execute("DELETE FROM analysis_record WHERE analysis_date = %s", (DAY,))
            c.execute("DELETE FROM analysis_batch WHERE analysis_date = %s", (DAY,))
            c.execute("DELETE FROM station_config WHERE station_id LIKE 'TST%%'")

    cleanup()
    pool = PgPool(dsn)
    yield PgAnalysisStore(pool), PgStationStore(pool)
    await pool.close()
    cleanup()


def batch(revision: int, status=BatchStatus.RUNNING) -> Batch:
    return Batch(batch_id=f"T{DAY:%Y%m%d}-r{revision}", analysis_date=DAY, version="vt",
                 revision=revision, status=status, trigger="manual", attempts=1,
                 timezone="Asia/Shanghai", window_start=NOW - timedelta(days=1),
                 window_end=NOW, created_at=NOW, config_snapshot={"stations": [{"x": 1}]})


def record(batch_id: str, device: str, level: Level) -> Record:
    rid = f"rec_{batch_id}_{device}"[:40]
    ev = Event(event_id=f"evt_{rid}"[:40], record_id=rid, code="E1", kind="dose_h1",
               title="剂量率连续超一级阈值", level=level, source=Source.RULE,
               category=Category.DOSE, start=NOW - timedelta(hours=3),
               end=NOW - timedelta(hours=2), duration_s=3600, sample_count=3,
               expected_samples=3, completeness=1.0, unit="µGy/h", min=0.3, max=0.4, mean=0.35,
               std=0.04, rule={"id": "dose.x", "threshold": 0.25},
               comparisons={"previous_day": Comparison(status="unavailable", reason="无")},
               context={"ac_power": {"status": "normal"}}, corroborated_by_source=True,
               values=[(NOW - timedelta(hours=3), 0.3), (NOW - timedelta(hours=2), 0.4)])
    ex = Explanation(summary="摘要", details="说明", method="template", confidence=0.7,
                     generated_at=NOW)
    return Record(record_id=rid, batch_id=batch_id, analysis_date=DAY, station_id="TST1",
                  station_name="测试站", equipment_type=EquipmentType.DETECTOR, device_id=device,
                  device_name=device, level=level, level_label="", title="t",
                  sources=[Source.RULE], event_count=1, data_status="complete",
                  day_stats={"completeness": 1.0}, explanation=ex, created_at=NOW, events=[ev])


async def test_batch_record_event_round_trip(stores):
    store, _ = stores
    b1 = batch(1)
    await store.create_batch(b1)
    with pytest.raises(Exception):  # noqa: B017 - unique (date, version, revision)
        await store.create_batch(b1.model_copy(update={"batch_id": "other"}))
    await store.save_record(record(b1.batch_id, "D1", Level.WATCH))
    await store.save_record(record(b1.batch_id, "D2", Level.INSPECT))
    assert await store.current_batch("vt", DAY) is None  # still running: hidden
    assert [b.batch_id for b in await store.running_batches()] == [b1.batch_id]
    await store.update_batch(b1.batch_id, status=BatchStatus.PARTIAL, finished_at=NOW,
                             counts={"records": 2}, errors=[{"device_id": "D3", "error": "x"}])
    cur = await store.current_batch("vt", DAY)
    assert cur.batch_id == b1.batch_id and cur.is_current and cur.counts == {"records": 2}
    assert cur.config_snapshot is None
    assert (await store.get_batch(b1.batch_id, snapshot=True)).config_snapshot == \
        {"stations": [{"x": 1}]}
    items, total = await store.list_records(b1.batch_id, {}, 10, 0)
    assert total == 2 and [r.device_id for r in items] == ["D2", "D1"]  # most severe first
    assert items[0].level_label == "需检修" and items[0].explanation.confidence == 0.7
    items, total = await store.list_records(b1.batch_id, {"level": Level.WATCH}, 10, 0)
    assert total == 1
    full = await store.get_record(items[0].record_id)
    [ev] = full.events
    assert ev.values[1][1] == 0.4 and ev.comparisons["previous_day"].status == "unavailable"
    assert ev.rule["threshold"] == 0.25 and ev.context["ac_power"]["status"] == "normal"
    ex = Explanation(summary="新", details="d", method="llm", confidence=0.9, generated_at=NOW)
    await store.set_explanation(full.record_id, ex)
    assert (await store.get_record(full.record_id)).explanation.method == "llm"
    # a second revision becomes current once done
    b2 = batch(2)
    await store.create_batch(b2)
    await store.update_batch(b2.batch_id, status=BatchStatus.SUCCEEDED, finished_at=NOW)
    page, total = await store.list_batches("vt", DAY, DAY, None, 10, 0)
    assert total == 2 and [(b.revision, b.is_current) for b in page] == [(2, True), (1, False)]
    assert [b.revision for b in await store.batches_for(DAY, "vt")] == [1, 2]
    await store.delete_records(b1.batch_id)
    assert (await store.list_records(b1.batch_id, {}, 10, 0))[1] == 0


async def test_station_store_round_trip(stores, stations):
    _, st = stores
    s = stations[0].model_copy(update={"station_id": "TST1"})
    assert await st.upsert([s]) == {"TST1": "created"}
    assert await st.upsert([s]) == {"TST1": "unchanged"}
    got = await st.get("TST1")
    assert got.dose_l1 == s.dose_l1 and [d.device_id for d in got.devices] == \
        [d.device_id for d in s.devices]
    assert got.devices[-1].enabled is False and "simulation" not in got.model_dump()
    assert await st.upsert([s.model_copy(update={"dose_l1": 0.2})], overwrite=False) == \
        {"TST1": "kept"}
    assert await st.upsert([s.model_copy(update={"dose_l1": 0.2})]) == {"TST1": "updated"}
    assert (await st.versions())["TST1"] == 2
