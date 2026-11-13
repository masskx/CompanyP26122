"""Internal maintenance interfaces for the main server (/internal/v1).

POST /internal/v1/maintenance/analyses              synchronous analysis (no SSE, nothing stored)
PUT  /internal/v1/maintenance/work-orders/{id}      reserved: the main server pushes its work
                                                    orders / published results / reviews here
GET  /internal/v1/maintenance/work-orders[/{id}]    read the copy (debugging, assistant)
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.db.work_orders import WorkOrder
from app.deps import Resources, get_resources
from app.maintenance.schemas import AnalysisError, AnalysisRequest, AnalysisResult

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.post("/analyses", response_model=AnalysisResult)
async def analyse(body: AnalysisRequest, request: Request,
                  idempotency_key: str | None = Header(default=None, max_length=128),
                  r: Resources = Depends(get_resources)):
    """Run one detector / backup-power / general analysis and return the final structured
    result (health_level, explanation, findings, actions, limitations, data_snapshot,
    versions). Errors: 404 site / equipment, 409 idempotency conflict, 422 window,
    503 watermark not reached or time series unavailable (retryable)."""
    try:
        return await r.maintenance.analyze(body, idempotency_key)
    except AnalysisError as exc:
        return JSONResponse({"code": exc.code, "message": exc.message,
                             "retryable": exc.retryable, "analysis_id": body.analysis_id,
                             "request_id": request.headers.get("x-request-id"), **exc.extra},
                            status_code=exc.status)


@router.put("/work-orders/{event_id}")
async def put_work_order(event_id: str, body: WorkOrder, r: Resources = Depends(get_resources)):
    """Store (create / update) the copy of one work order. Older updates (updated_at) are
    ignored and reported as "stale"."""
    if body.event_id != event_id:
        raise HTTPException(422, "event_id in the path and the body differ")
    return {"event_id": event_id, "result": await r.work_orders.upsert(body)}


@router.get("/work-orders/{event_id}", response_model=WorkOrder)
async def get_work_order(event_id: str, r: Resources = Depends(get_resources)):
    wo = await r.work_orders.get(event_id)
    if wo is None:
        raise HTTPException(404, f"work order {event_id} not found")
    return wo


@router.get("/work-orders")
async def list_work_orders(site_id: str | None = None, equipment_id: str | None = None,
                           start: datetime | None = Query(None, alias="from"),
                           end: datetime | None = Query(None, alias="to"),
                           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                           r: Resources = Depends(get_resources)):
    items = await r.work_orders.list(site_id, equipment_id, start, end, limit, offset)
    return {"items": items, "next_offset": offset + len(items) if len(items) == limit else None}
