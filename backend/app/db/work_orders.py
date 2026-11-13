"""Copy of the main server's maintenance work orders (Question.txt: "工单这部分内容应该在我的
openGauss 也存储一份，这个你要预留一个接口").

The main server stays authoritative: it creates work orders from analysis results and pushes
each create / update / review here (PUT /internal/v1/maintenance/work-orders/{event_id}).
The copy is read by the analysis history rules and by the assistant. No user identity is
stored (reviewer ids are not accepted), matching the de-identified history contract.
"""

import json
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.db.pool import PgPool


class Review(BaseModel):
    outcome: Literal["accurate", "needs_revision"]
    comment: str = Field(default="", max_length=2000)
    reviewed_at: datetime
    updated_at: datetime | None = None


class WorkOrder(BaseModel):
    event_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w.:\-]+$")
    site_id: str = Field(min_length=1, max_length=64)
    equipment_id: str = Field(min_length=1, max_length=64)
    risk_type: Literal["detector", "backup_power", "general"]
    health_level: Literal["watch", "inspect", "insufficient_data"]
    status: Literal["pending", "confirmed"] | None = None
    triggered_at: datetime
    analysis_id: str | None = Field(default=None, max_length=64)
    ai_result: dict | None = None   # published result (explanation, findings, actions, ...)
    review: Review | None = None
    updated_at: datetime | None = None  # main server's last change; older pushes are ignored
    received_at: datetime | None = None


class WorkOrderStore(Protocol):
    async def ping(self) -> None: ...
    async def upsert(self, wo: WorkOrder) -> str: ...
    async def get(self, event_id: str) -> WorkOrder | None: ...
    async def list(self, site_id: str | None, equipment_id: str | None, start: datetime | None,
                   end: datetime | None, limit: int, offset: int = 0) -> list[WorkOrder]: ...
    async def close(self) -> None: ...


def _newer(incoming: WorkOrder, stored: WorkOrder) -> bool:
    if incoming.updated_at is None or stored.updated_at is None:
        return True
    return incoming.updated_at >= stored.updated_at


class MemoryWorkOrderStore:
    def __init__(self) -> None:
        self.items: dict[str, WorkOrder] = {}

    async def ping(self) -> None:
        return None

    async def upsert(self, wo: WorkOrder) -> str:
        old = self.items.get(wo.event_id)
        if old is not None and not _newer(wo, old):
            return "stale"
        self.items[wo.event_id] = wo.model_copy(update={"received_at": datetime.now(UTC)})
        return "updated" if old else "created"

    async def get(self, event_id: str) -> WorkOrder | None:
        return self.items.get(event_id)

    async def list(self, site_id, equipment_id, start, end, limit, offset=0):
        items = [w for w in self.items.values()
                 if (site_id is None or w.site_id == site_id)
                 and (equipment_id is None or w.equipment_id == equipment_id)
                 and (start is None or w.triggered_at >= start)
                 and (end is None or w.triggered_at <= end)]
        items.sort(key=lambda w: w.triggered_at, reverse=True)
        return items[offset:offset + limit]

    async def close(self) -> None:
        return None


_COLS = ("event_id, site_id, equipment_id, risk_type, health_level, status, triggered_at, "
         "analysis_id, ai_result, review, source_updated_at, received_at")


def _row(r: dict) -> WorkOrder:
    return WorkOrder(event_id=r["event_id"], site_id=r["site_id"],
                     equipment_id=r["equipment_id"], risk_type=r["risk_type"],
                     health_level=r["health_level"], status=r["status"],
                     triggered_at=r["triggered_at"], analysis_id=r["analysis_id"],
                     ai_result=json.loads(r["ai_result"]) if r["ai_result"] else None,
                     review=json.loads(r["review"]) if r["review"] else None,
                     updated_at=r["source_updated_at"], received_at=r["received_at"])


class PgWorkOrderStore:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def ping(self) -> None:
        async with await self._pool.connection() as conn:
            await conn.execute("SELECT 1 FROM maintenance_work_order WHERE 1 = 0")

    async def upsert(self, wo: WorkOrder) -> str:
        now = datetime.now(UTC)
        values = (wo.site_id, wo.equipment_id, wo.risk_type, wo.health_level, wo.status,
                  wo.triggered_at, wo.analysis_id,
                  json.dumps(wo.ai_result, ensure_ascii=False) if wo.ai_result else None,
                  wo.review.model_dump_json() if wo.review else None, wo.updated_at, now)
        async with await self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                "SELECT source_updated_at FROM maintenance_work_order WHERE event_id = %s "
                "FOR UPDATE", (wo.event_id,))).fetchone()
            if row is None:
                await conn.execute(f"INSERT INTO maintenance_work_order ({_COLS}) VALUES "
                                   f"({', '.join(['%s'] * 12)})", (wo.event_id, *values))
                return "created"
            stored = row["source_updated_at"]
            if wo.updated_at is not None and stored is not None and wo.updated_at < stored:
                return "stale"
            await conn.execute(
                "UPDATE maintenance_work_order SET site_id = %s, equipment_id = %s, "
                "risk_type = %s, health_level = %s, status = %s, triggered_at = %s, "
                "analysis_id = %s, ai_result = %s, review = %s, source_updated_at = %s, "
                "received_at = %s WHERE event_id = %s", (*values, wo.event_id))
            return "updated"

    async def get(self, event_id: str) -> WorkOrder | None:
        async with await self._pool.connection() as conn:
            row = await (await conn.execute(
                f"SELECT {_COLS} FROM maintenance_work_order WHERE event_id = %s",
                (event_id,))).fetchone()
        return _row(row) if row else None

    async def list(self, site_id, equipment_id, start, end, limit, offset=0):
        where, params = ["1 = 1"], []
        for cond, value in (("site_id = %s", site_id), ("equipment_id = %s", equipment_id),
                            ("triggered_at >= %s", start), ("triggered_at <= %s", end)):
            if value is not None:
                where.append(cond)
                params.append(value)
        async with await self._pool.connection() as conn:
            rows = await (await conn.execute(
                f"SELECT {_COLS} FROM maintenance_work_order WHERE {' AND '.join(where)} "
                "ORDER BY triggered_at DESC LIMIT %s OFFSET %s",
                [*params, limit, offset])).fetchall()
        return [_row(r) for r in rows]

    async def close(self) -> None:
        return None
