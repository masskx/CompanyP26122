"""Work-order copy and report-record stores: memory always, openGauss when
OPENGAUSS_TEST_DSN is set (rows created by the test are removed)."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.reports import MemoryReportStore, ReportRecord
from app.db.work_orders import MemoryWorkOrderStore, Review, WorkOrder

pytestmark = pytest.mark.anyio
DSN = os.environ.get("OPENGAUSS_TEST_DSN")
T0 = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "opengauss"])
async def stores(request):
    if request.param == "memory":
        yield MemoryWorkOrderStore(), MemoryReportStore()
        return
    if not DSN:
        pytest.skip("OPENGAUSS_TEST_DSN not set")
    import psycopg

    from app.agent.reports import PgReportStore
    from app.db.migrate import migrate
    from app.db.pool import PgPool
    from app.db.work_orders import PgWorkOrderStore

    await migrate(DSN)

    def cleanup():
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("DELETE FROM maintenance_work_order WHERE event_id LIKE 'tst-%%'")
            c.execute("DELETE FROM assistant_report WHERE report_id LIKE 'tst-%%'")

    cleanup()
    pool = PgPool(DSN)
    yield PgWorkOrderStore(pool), PgReportStore(pool)
    await pool.close()
    cleanup()


def order(i: int, **kw) -> WorkOrder:
    return WorkOrder.model_validate({
        "event_id": f"tst-{i}", "site_id": "AS06", "equipment_id": "AS06-BAT-01",
        "risk_type": "backup_power", "health_level": "inspect", "status": "pending",
        "triggered_at": T0 + timedelta(hours=i), "analysis_id": f"ma-{i}",
        "ai_result": {"explanation": "参考续航下降",
                      "findings": [{"rule_id": "BACKUP-RUNTIME-DROP"}]},
        "updated_at": T0 + timedelta(hours=i), **kw})


async def test_work_orders(stores):
    wo, _ = stores
    await wo.ping()
    assert await wo.upsert(order(1)) == "created"
    assert await wo.upsert(order(2)) == "created"
    reviewed = order(1, status="confirmed", updated_at=T0 + timedelta(days=1),
                     review={"outcome": "accurate", "comment": "已核查",
                             "reviewed_at": T0 + timedelta(days=1)})
    assert await wo.upsert(reviewed) == "updated"
    assert await wo.upsert(order(1, status="pending")) == "stale"   # older than stored
    got = await wo.get("tst-1")
    assert got.status == "confirmed" and got.review.outcome == "accurate"
    assert got.ai_result["findings"][0]["rule_id"] == "BACKUP-RUNTIME-DROP"
    items = await wo.list("AS06", "AS06-BAT-01", T0, T0 + timedelta(days=2), 10)
    assert [w.event_id for w in items if w.event_id.startswith("tst-")] == ["tst-2", "tst-1"]
    assert await wo.list("AS06", "AS06-BAT-01", T0 + timedelta(hours=2), None, 10, 0) != []
    assert await wo.get("tst-404") is None


def review(outcome: str, at: datetime, archived: datetime | None = None, **kw) -> Review:
    return Review(outcome=outcome, comment=kw.get("comment", ""), reviewed_at=at,
                  updated_at=at, archived_at=archived or at)


async def count_rows(wo, event_id: str) -> int:
    if isinstance(wo, MemoryWorkOrderStore):
        return sum(1 for k in wo.items if k == event_id)
    import psycopg

    with psycopg.connect(DSN) as c:
        return c.execute("SELECT count(*) FROM maintenance_work_order WHERE event_id = %s",
                         (event_id,)).fetchone()[0]


async def test_set_review(stores):
    wo, _ = stores
    assert await wo.upsert(order(5)) == "created"
    t1 = T0 + timedelta(days=1)
    before = datetime.now(UTC)
    assert await wo.set_review("tst-5", review("accurate", t1, comment="已核查"), "confirmed",
                               t1) == "updated"
    got = await wo.get("tst-5")
    assert got.status == "confirmed" and got.updated_at == t1
    assert got.review == review("accurate", t1, comment="已核查")
    assert got.received_at >= before - timedelta(seconds=1)
    assert got.ai_result["findings"][0]["rule_id"] == "BACKUP-RUNTIME-DROP"  # untouched
    assert got.health_level == "inspect" and got.analysis_id == "ma-5"

    # edit after archiving: overwritten in place, archived_at kept, no second row
    t2 = t1 + timedelta(hours=3)
    edited = review("needs_revision", t2, archived=t1, comment="补充说明")
    assert await wo.set_review("tst-5", edited, "confirmed", t2) == "updated"
    got = await wo.get("tst-5")
    assert got.review == edited and got.review.archived_at == t1 and got.updated_at == t2
    assert await count_rows(wo, "tst-5") == 1

    # equal updated_at may overwrite; older is stale and changes nothing
    again = review("accurate", t2, archived=t1)
    assert await wo.set_review("tst-5", again, "confirmed", t2) == "updated"
    older = review("needs_revision", t1, comment="old")
    assert await wo.set_review("tst-5", older, "confirmed", t1) == "stale"
    assert (await wo.get("tst-5")).review == again
    assert await wo.set_review("tst-404", again, "confirmed", t2) == "not_found"
    assert await wo.get("tst-404") is None


async def test_report_records(stores):
    _, reports = stores
    rec = ReportRecord(report_id="tst-r1", user_id="client:abc", session_id=None, title="t",
                       params={"site_ids": ["AS01"]}, file_name="r.xlsx", size_bytes=10,
                       row_count=2, created_at=T0)
    await reports.add(rec)
    assert (await reports.get("client:abc", "tst-r1")).params == {"site_ids": ["AS01"]}
    assert await reports.get("someone", "tst-r1") is None
    assert await reports.reassign("client:abc", "zhang.san") >= 1
    assert [r.report_id for r in await reports.list("zhang.san", 10, 0)
            if r.report_id.startswith("tst-")] == ["tst-r1"]
