"""Station data reads shared by the REST API and the agent's tools (read-only)."""

import asyncio
from datetime import datetime

from app.stations.model import (
    CODE_STATUS,
    DEVICE_METRICS,
    STATION_METRICS,
    STATION_NODE,
    UNITS,
    Metric,
    Station,
)
from app.stations.store import StationStore
from app.timeseries.iotdb import Point, TimeSeriesStore

_FLAGS = {Metric.COMM: ("ok", "interrupted"), Metric.AC_POWER: ("on", "off"),
          Metric.CHARGING: ("charging", "not_charging"), Metric.DOOR: ("open", "closed")}


class StationNotFound(LookupError):
    pass


def readable(metric: Metric, p: Point | None) -> dict | None:
    """A stored sample as the API shows it (codes -> labels, units)."""
    if p is None or p.value is None:
        return None
    out: dict = {"time": p.time.isoformat()}
    if metric == Metric.STATUS:
        status = CODE_STATUS.get(int(p.value))
        out["value"] = status.value if status else f"unknown({p.value:g})"
    elif metric in _FLAGS:
        on, off = _FLAGS[metric]
        out["value"] = on if p.value == 1 else off
    else:
        out["value"], out["unit"] = p.value, UNITS.get(metric)
    return out


class StationService:
    def __init__(self, stations: StationStore, store: TimeSeriesStore) -> None:
        self.stations, self.store = stations, store

    async def station(self, station_id: str) -> Station:
        s = await self.stations.get(station_id)
        if s is None:
            raise StationNotFound(station_id)
        return s

    async def latest(self, station: Station) -> dict:
        """Latest stored value of every series of the station (as reported, not calibrated)."""
        async def one(node: str, metric: Metric) -> dict | None:
            return readable(metric, await self.store.latest(station.station_id, node,
                                                            metric.value))

        devices = []
        for d in station.devices:
            values = await asyncio.gather(*(one(d.device_id, m) for m in DEVICE_METRICS))
            devices.append({"device_id": d.device_id, "name": d.name, "enabled": d.enabled,
                            **{m.value: v for m, v in zip(DEVICE_METRICS, values,
                                                          strict=True)}})
        values = await asyncio.gather(*(one(STATION_NODE, m) for m in STATION_METRICS))
        return {"devices": devices,
                "station": {m.value: v for m, v in zip(STATION_METRICS, values, strict=True)}}

    async def telemetry(self, station: Station, device_id: str | None, metric: Metric,
                        start: datetime, end: datetime, interval_seconds: int | None) -> dict:
        node = device_id or STATION_NODE
        if device_id is not None and station.device(device_id) is None:
            raise StationNotFound(f"{station.station_id}/{device_id}")
        expected = DEVICE_METRICS if device_id else STATION_METRICS
        if metric not in expected:
            raise ValueError(f"metric {metric.value} is not a "
                             f"{'device' if device_id else 'station'} metric")
        if interval_seconds:
            buckets = await self.store.aggregate(station.station_id, node, metric.value, start,
                                                 end, interval_seconds)
            data = [{"time": b.time.isoformat(), "avg": b.avg, "min": b.min, "max": b.max,
                     "count": b.count} for b in buckets]
        else:
            points = await self.store.query(station.station_id, node, metric.value, start, end,
                                            limit=10_000)
            data = [{"time": p.time.isoformat(), "value": p.value} for p in points]
        return {"station_id": station.station_id, "device_id": device_id,
                "metric": metric.value, "unit": UNITS.get(metric),
                "calibration_factor": station.calibration_factor
                if metric == Metric.DOSE_RATE else None,
                "interval_seconds": interval_seconds, "points": data}
