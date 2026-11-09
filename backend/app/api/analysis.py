"""Daily station analysis results.

GET requests only read openGauss (never run rules or the model). A re-run is requested with
POST /analysis/batches and then polled with GET /analysis/batches/{batch_id}.
"""

import datetime as dt
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.analysis.models import BatchStatus, EquipmentType, Level
from app.deps import Resources, get_resources

router = APIRouter(prefix="/analysis", tags=["analysis"])


class BatchRequest(BaseModel):
    date: dt.date | None = None  # default: yesterday (ANALYSIS_TIMEZONE)
    force: bool = False       # True: new revision even if the day was analysed already


@router.get("/status")
async def status(r: Resources = Depends(get_resources)):
    """Schedule, yesterday's batch, the latest available batch, running batches."""
    return await r.analysis.status(r.scheduler)


@router.get("/batches")
async def list_batches(date_from: date | None = None, date_to: date | None = None,
                       status: BatchStatus | None = None,
                       limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                       r: Resources = Depends(get_resources)):
    items, total = await r.analysis.store.list_batches(
        r.analysis.version, date_from, date_to, status.value if status else None, limit, offset)
    return {"total": total, "items": items}


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, include_snapshot: bool = False,
                    r: Resources = Depends(get_resources)):
    """Batch status (poll this after POST), counts, configuration gaps, errors and,
    optionally, the configuration snapshot used."""
    b = await r.analysis.store.get_batch(batch_id, snapshot=include_snapshot)
    if b is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return b


@router.post("/batches", status_code=202)
async def create_batch(body: BatchRequest, r: Resources = Depends(get_resources)):
    """Run (or re-run with force=true) the analysis of one finished day. 202 when a run
    started, 200 when an existing batch is returned (same date and version)."""
    day = body.date or r.analysis.today() - timedelta(days=1)
    try:
        batch, started = await r.analysis.request(day, trigger="manual", force=body.force)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    payload = {"batch": batch.model_dump(mode="json"), "started": started,
               "poll": f"/api/v1/analysis/batches/{batch.batch_id}"}
    return JSONResponse(payload, status_code=202 if started else 200)


@router.get("/records")
async def list_records(batch_id: str | None = Query(None, description="default: current batch"),
                       analysis_date: date | None = Query(None, alias="date",
                                                          description="current batch of a day"),
                       station_id: str | None = None, device_id: str | None = None,
                       equipment_type: EquipmentType | None = None,
                       level: Level | None = None,
                       limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       r: Resources = Depends(get_resources)):
    """Abnormal records (one per device / station battery), most severe first, without
    event values. Default: the latest available day ("yesterday's analysis")."""
    store = r.analysis.store
    if batch_id:
        batch = await store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(404, f"batch {batch_id} not found")
    else:
        batch = await store.current_batch(r.analysis.version, analysis_date)
    if batch is None:
        return {"batch": None, "total": 0, "items": []}
    items, total = await store.list_records(
        batch.batch_id, {"station_id": station_id, "device_id": device_id,
                         "equipment_type": equipment_type, "level": level}, limit, offset)
    return {"batch": batch, "total": total, "items": items}


@router.get("/records/{record_id}")
async def get_record(record_id: str, include_values: bool = True,
                     r: Resources = Depends(get_resources)):
    """One record with all events (statistics, rule, thresholds, comparisons, context) and
    the samples inside every abnormal interval."""
    record = await r.analysis.store.get_record(record_id)
    if record is None:
        raise HTTPException(404, f"record {record_id} not found")
    if not include_values:
        for e in record.events or []:
            e.values = None
    return record
