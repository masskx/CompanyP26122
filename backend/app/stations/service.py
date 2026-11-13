"""Station data reads shared by the REST API, the analysis and the assistant (read-only)."""

import asyncio
from datetime import datetime

from app.stations.model import (
    CODE_STATUS,
    METRIC_TYPE,
    METRICS_BY_TYPE,
    UNITS,
    EquipmentType,
    Metric,
    Station,
)
from app.stations.store import StationStore
from app.timeseries.iotdb import WATERMARK_SERIES, Point, TimeSeriesStore

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

    async def watermark(self) -> int | None:
        """Consumption watermark written by the MQTT ingester (None = never written)."""
        p = await self.store.latest(*WATERMARK_SERIES)
        return int(p.value) if p and p.value is not None else None

    async def latest(self, station: Station) -> dict:
        """Latest stored value of every series of the station (as reported, not calibrated)."""
        async def one(equipment: str, metric: Metric) -> dict | None:
            return readable(metric, await self.store.latest(station.station_id, equipment,
                                                            metric.value))

        async def unit(equipment: str, kind: EquipmentType) -> dict:
            metrics = METRICS_BY_TYPE[kind]
            values = await asyncio.gather(*(one(equipment, m) for m in metrics))
            return {m.value: v for m, v in zip(metrics, values, strict=True)}

        devices = [{"device_id": d.device_id, "name": d.name, "enabled": d.enabled,
                    **await unit(d.device_id, EquipmentType.DETECTOR)} for d in station.devices]
        return {"devices": devices,
                "backup_power": {"equipment_id": station.backup_power_id,
                                 **await unit(station.backup_power_id,  # type: ignore[arg-type]
                                              EquipmentType.BACKUP_POWER)},
                "environment": {"equipment_id": station.environment_id,
                                **await unit(station.environment_id,  # type: ignore[arg-type]
                                             EquipmentType.ENVIRONMENT)}}

    async def telemetry(self, station: Station, device_id: str | None, metric: Metric,
                        start: datetime, end: datetime, interval_seconds: int | None) -> dict:
        kind = METRIC_TYPE[metric]
        if kind is EquipmentType.DETECTOR:
            if device_id is None or station.device(device_id) is None:
                raise StationNotFound(f"{station.station_id}/{device_id}")
        elif device_id is not None and station.equipment_type(device_id) is not kind:
            raise ValueError(f"metric {metric.value} belongs to the station's {kind.value} unit")
        node = station.node_for(metric, device_id)
        if interval_seconds:
            buckets = await self.store.aggregate(station.station_id, node, metric.value, start,
                                                 end, interval_seconds)
            data = [{"time": b.time.isoformat(), "avg": b.avg, "min": b.min, "max": b.max,
                     "count": b.count} for b in buckets]
        else:
            points = await self.store.query(station.station_id, node, metric.value, start, end,
                                            limit=10_000)
            data = [{"time": p.time.isoformat(), "value": p.value} for p in points]
        return {"station_id": station.station_id, "equipment_id": node,
                "metric": metric.value, "unit": UNITS.get(metric),
                "calibration_factor": station.calibration_factor
                if metric == Metric.DOSE_RATE else None,
                "interval_seconds": interval_seconds, "points": data}
