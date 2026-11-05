"""Device data operations shared by the REST API and the agent's tools."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.config import Settings
from app.timeseries.devices import Device, DeviceCatalog, Level, assess_metric, worst
from app.timeseries.iotdb import Bucket, Measurement, Point


class TimeSeriesStore(Protocol):
    async def ping(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, batch: list[Measurement]) -> None: ...
    async def query(self, site: str, device_id: str, metric: str, start: datetime,
                    end: datetime, limit: int | None = None) -> list[Point]: ...
    async def aggregate(self, site: str, device_id: str, metric: str, start: datetime,
                        end: datetime, interval_seconds: int) -> list[Bucket]: ...
    async def latest(self, site: str, device_id: str, metric: str) -> Point | None: ...
    async def ensure_schema(self, series: list[tuple[str, str, str]]) -> dict[str, int]: ...
    async def delete(self, series: list[tuple[str, str, str]]) -> int: ...


class DeviceNotFound(LookupError):
    pass


def window(hours: float, end: datetime | None = None) -> tuple[datetime, datetime]:
    end = end or datetime.now(UTC)
    return end - timedelta(hours=hours), end


class DeviceService:
    def __init__(self, catalog: DeviceCatalog, store: TimeSeriesStore, settings: Settings):
        self.catalog = catalog
        self.store = store
        self._s = settings
        self._sem = asyncio.Semaphore(8)

    def device(self, device_id: str) -> Device:
        device = self.catalog.get(device_id)
        if device is None:
            raise DeviceNotFound(device_id)
        return device

    async def latest(self, device: Device) -> dict[str, dict | None]:
        out = {}
        for m in device.metrics:
            p = await self.store.latest(device.site, device.device_id, m.name)
            out[m.name] = {"time": p.time.isoformat(), "value": p.value, "unit": m.unit} \
                if p else None
        return out

    async def assess(self, device: Device, start: datetime, end: datetime) -> dict:
        results = []
        for m in device.metrics:
            async with self._sem:
                points = await self.store.query(device.site, device.device_id, m.name, start, end)
            results.append(assess_metric(m, points, start, end,
                                         min_completeness=self._s.rule_min_completeness,
                                         consecutive=self._s.rule_consecutive_breaches))
        return {
            "device_id": device.device_id, "name": device.name, "site": device.site,
            "type": device.type, "window": {"start": start.isoformat(), "end": end.isoformat()},
            "level": worst([r.level for r in results]).value,
            "metrics": [r.to_dict() for r in results],
            "thresholds": {m.name: m.model_dump(exclude={"name", "interval_seconds"})
                           for m in device.metrics},
        }

    async def overview(self, site: str | None, hours: float) -> dict:
        start, end = window(hours)
        devices = self.catalog.all(site=site)
        items = await asyncio.gather(*(self.assess(d, start, end) for d in devices))
        counts = {lv.value: 0 for lv in Level}
        for item in items:
            counts[item["level"]] += 1
        return {"site": site, "window": {"start": start.isoformat(), "end": end.isoformat()},
                "counts": counts,
                "devices": [{"device_id": i["device_id"], "name": i["name"], "type": i["type"],
                             "level": i["level"],
                             "findings": [f for m in i["metrics"] for f in m["findings"]]}
                            for i in items]}

    async def telemetry(self, device: Device, metric: str, start: datetime, end: datetime,
                        interval_seconds: int | None) -> dict:
        m = device.metric(metric)
        if m is None:
            raise DeviceNotFound(f"{device.device_id}/{metric}")
        if interval_seconds:
            buckets = await self.store.aggregate(device.site, device.device_id, metric, start,
                                                 end, interval_seconds)
            data = [{"time": b.time.isoformat(), "avg": b.avg, "min": b.min, "max": b.max,
                     "count": b.count} for b in buckets]
        else:
            points = await self.store.query(device.site, device.device_id, metric, start, end,
                                            limit=10_000)
            data = [{"time": p.time.isoformat(), "value": p.value} for p in points]
        return {"device_id": device.device_id, "metric": metric, "unit": m.unit,
                "interval_seconds": interval_seconds, "points": data}
