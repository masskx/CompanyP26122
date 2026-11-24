"""Station configuration and raw station data (read-only)."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import Resources, get_resources
from app.stations.model import Metric
from app.stations.service import StationNotFound
from app.timeseries.iotdb import TimeSeriesError

router = APIRouter(tags=["stations"])


def _unavailable(exc: TimeSeriesError) -> HTTPException:
    return HTTPException(503, f"time-series database unavailable: {exc}")


async def _station(r: Resources, station_id: str):
    try:
        return await r.station_data.station(station_id)
    except StationNotFound:
        raise HTTPException(404, f"station {station_id} not found") from None


@router.get("/stations")
async def list_stations(include_disabled: bool = False, r: Resources = Depends(get_resources)):
    """Station configuration (thresholds, calibration factor, device directory)."""
    return [s.model_dump(mode="json") | {"dose_configured": s.dose_configured,
                                         "battery_configured": s.battery_configured}
            for s in await r.stations.all(include_disabled=include_disabled)]


@router.get("/stations/{station_id}")
async def get_station(station_id: str, latest: bool = Query(True, description="latest values"),
                      r: Resources = Depends(get_resources)):
    s = await _station(r, station_id)
    out = s.model_dump(mode="json") | {"dose_configured": s.dose_configured,
                            "battery_configured": s.battery_configured}
    if latest:
        try:
            out["latest"] = await r.station_data.latest(s)
        except TimeSeriesError as exc:
            raise _unavailable(exc) from exc
    return out


@router.get("/stations/{station_id}/telemetry")
async def telemetry(station_id: str,
                    metric: Metric = Query(Metric.DOSE_RATE),
                    device_id: str | None = Query(
                        None, description="detector id for detector metrics; battery / "
                                          "environment metrics use the station's unit"),
                    start: datetime | None = Query(None, description="default: end - 24 h"),
                    end: datetime | None = Query(None, description="default: now"),
                    interval: int | None = Query(None, ge=1, le=86400,
                                                 description="aggregate into N-second buckets"),
                    r: Resources = Depends(get_resources)):
    """Stored samples (up to 10 000; dose rate as reported, multiply by calibration_factor) or,
    with `interval`, avg/min/max/count per bucket."""
    s = await _station(r, station_id)
    end = end or datetime.now(UTC)
    start = start or end - timedelta(hours=24)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise HTTPException(422, "start/end must include a time zone and start < end")
    if end - start > timedelta(days=31):
        raise HTTPException(422, "time range is limited to 31 days")
    try:
        return await r.station_data.telemetry(s, device_id, metric, start, end, interval)
    except StationNotFound:
        raise HTTPException(404, f"detector {device_id} not found at {station_id}") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except TimeSeriesError as exc:
        raise _unavailable(exc) from exc


# ---- auxiliary sources: monitoring vehicle, weather tower (stored as reported) ------------

async def _source(r: Resources, source_id: str):
    a = await r.stations.auxiliary_source(source_id)
    if a is None:
        raise HTTPException(404, f"auxiliary source {source_id} not found")
    return a


@router.get("/auxiliary")
async def list_auxiliary(r: Resources = Depends(get_resources)):
    """Monitoring vehicle, weather tower, ...: channels as declared in STATIONS_FILE."""
    return [a.model_dump(mode="json") for a in await r.stations.auxiliary()]


@router.get("/auxiliary/{source_id}")
async def get_auxiliary(source_id: str, r: Resources = Depends(get_resources)):
    """The source with the latest value of every channel (no unit conversion)."""
    a = await _source(r, source_id)
    try:
        return a.model_dump(mode="json") | {"latest": await r.station_data.auxiliary_latest(a)}
    except TimeSeriesError as exc:
        raise _unavailable(exc) from exc


@router.get("/auxiliary/{source_id}/telemetry")
async def auxiliary_telemetry(
        source_id: str,
        field: str = Query(..., description="channel, e.g. wind_speed_10"),
        start: datetime | None = Query(None, description="default: end - 24 h"),
        end: datetime | None = Query(None, description="default: now"),
        interval: int | None = Query(None, ge=1, le=86400),
        r: Resources = Depends(get_resources)):
    a = await _source(r, source_id)
    end = end or datetime.now(UTC)
    start = start or end - timedelta(hours=24)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise HTTPException(422, "start/end must include a time zone and start < end")
    if end - start > timedelta(days=31):
        raise HTTPException(422, "time range is limited to 31 days")
    try:
        return await r.station_data.auxiliary_telemetry(a, field, start, end, interval)
    except StationNotFound:
        raise HTTPException(404, f"channel {field} not found at {source_id}") from None
    except TimeSeriesError as exc:
        raise _unavailable(exc) from exc
