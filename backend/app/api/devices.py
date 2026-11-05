"""Devices and dose-rate data (read-only)."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import Resources, get_resources
from app.timeseries.iotdb import TimeSeriesError
from app.timeseries.service import DeviceNotFound, window

router = APIRouter(tags=["devices"])


def _device(r: Resources, device_id: str):
    try:
        return r.devices.device(device_id)
    except DeviceNotFound:
        raise HTTPException(404, f"device {device_id} not found") from None


def _unavailable(exc: TimeSeriesError) -> HTTPException:
    return HTTPException(503, f"time-series database unavailable: {exc}")


@router.get("/devices")
async def list_devices(site: str | None = None, type: str | None = None,
                       latest: bool = Query(True, description="include latest readings"),
                       r: Resources = Depends(get_resources)):
    out = []
    for d in r.devices.catalog.all(site=site, type=type):
        item = d.model_dump(exclude={"simulation"})
        if latest:
            try:
                item["latest"] = await r.devices.latest(d)
            except TimeSeriesError as exc:
                raise _unavailable(exc) from exc
        out.append(item)
    return out


@router.get("/devices/{device_id}")
async def get_device(device_id: str, r: Resources = Depends(get_resources)):
    d = _device(r, device_id)
    try:
        return {**d.model_dump(exclude={"simulation"}), "latest": await r.devices.latest(d)}
    except TimeSeriesError as exc:
        raise _unavailable(exc) from exc


@router.get("/devices/{device_id}/telemetry")
async def telemetry(device_id: str, metric: str = "dose_rate",
                    start: datetime | None = Query(None, description="default: end - 6 h"),
                    end: datetime | None = Query(None, description="default: now"),
                    interval: int | None = Query(None, ge=1, le=86400,
                                                 description="aggregate into N-second buckets"),
                    r: Resources = Depends(get_resources)):
    """Raw points (up to 10 000) or, with `interval`, avg/min/max/count per bucket."""
    d = _device(r, device_id)
    end = end or datetime.now(UTC)
    start = start or end - timedelta(hours=6)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise HTTPException(422, "start/end must include a time zone and start < end")
    if end - start > timedelta(days=31):
        raise HTTPException(422, "time range is limited to 31 days")
    try:
        return await r.devices.telemetry(d, metric, start, end, interval)
    except DeviceNotFound:
        raise HTTPException(404, f"metric {metric} not found") from None
    except TimeSeriesError as exc:
        raise _unavailable(exc) from exc


@router.get("/devices/{device_id}/assessment")
async def assessment(device_id: str, hours: float = Query(24, gt=0, le=168),
                     r: Resources = Depends(get_resources)):
    """Rule-based health level (normal / watch / inspect / insufficient_data) with statistics."""
    d = _device(r, device_id)
    try:
        return await r.devices.assess(d, *window(hours))
    except TimeSeriesError as exc:
        raise _unavailable(exc) from exc


@router.get("/overview")
async def overview(site: str | None = None, hours: float = Query(24, gt=0, le=168),
                   r: Resources = Depends(get_resources)):
    """Health level of every device (optionally one site)."""
    try:
        return await r.devices.overview(site, hours)
    except TimeSeriesError as exc:
        raise _unavailable(exc) from exc
