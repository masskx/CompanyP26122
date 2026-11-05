"""Synthetic radiation dose-rate data (development/testing only, not plant data).

Each device in data/devices.json may carry a "simulation" profile:
    {"baseline": 0.5,              µSv/h background
     "noise": 0.05,                relative noise at baseline (counting statistics: relative
                                   noise shrinks as the rate rises)
     "diurnal": 0.0,               relative daily variation
     "drift": 0.02,                slow random drift (per day)
     "events": [{"type": "elevation", "start_minutes": -150, "duration_minutes": 40,
                 "factor": 15}],   scripted, minutes relative to simulator start
     "random": {"spike_per_day": 0.5, "washout_per_day": 0.6, "dropout_per_day": 2}}
Event types: spike (one sample x factor), elevation (x factor for a duration),
washout (rain: +factor x baseline decaying with tau = duration), dropout (no readings),
stuck (last value repeats).

`SimulatedSource` emits the same (topic, payload) messages an MQTT broker would deliver.
"""

import asyncio
import heapq
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.ingestion.parsers import RawMessage, SimulatorFormat
from app.timeseries.devices import Device


@dataclass
class _Event:
    kind: str
    start: datetime
    end: datetime
    factor: float
    tau: float = 0.0


class DeviceSimulator:
    def __init__(self, device: Device, rng: random.Random, start: datetime, anchor: datetime):
        p = device.simulation or {}
        metric = device.metrics[0]
        self.device, self.metric = device, metric.name
        self.interval = timedelta(seconds=metric.interval_seconds or 60)
        self.baseline = float(p.get("baseline", 0.1))
        self.noise = float(p.get("noise", 0.05))
        self.diurnal = float(p.get("diurnal", 0.0))
        self.drift_sigma = float(p.get("drift", 0.02))
        self.random = p.get("random", {})
        self._rng = rng
        step = self.interval.total_seconds()
        self.next_time = datetime.fromtimestamp(math.ceil(start.timestamp() / step) * step, UTC)
        self._drift = 1.0
        self._last: float | None = None
        self._active: list[_Event] = []
        self._scripted = [self._event(e["type"], anchor + timedelta(minutes=e["start_minutes"]),
                                      e.get("duration_minutes", 0), e.get("factor", 1.0))
                          for e in p.get("events", [])]

    def _event(self, kind: str, start: datetime, minutes: float, factor: float) -> _Event:
        if kind == "spike":
            return _Event(kind, start, start + self.interval, factor)
        if kind == "washout":
            return _Event(kind, start, start + timedelta(minutes=5 * minutes), factor, minutes * 60)
        return _Event(kind, start, start + timedelta(minutes=minutes), factor)

    def _maybe_random(self, t: datetime) -> None:
        per_step = self.interval.total_seconds() / 86400
        r, rng = self.random, self._rng
        chances = {"spike": (rng.uniform(2, 4), 0), "elevation": (rng.uniform(1.5, 3),
                   rng.uniform(10, 60)), "washout": (rng.uniform(0.2, 0.6), rng.uniform(60, 180)),
                   "dropout": (1, rng.uniform(1, 10)), "stuck": (1, rng.uniform(10, 60))}
        for kind, (factor, minutes) in chances.items():
            if rng.random() < r.get(f"{kind}_per_day", 0) * per_step:
                self._active.append(self._event(kind, t, minutes, factor))

    def step(self) -> tuple[datetime, float | None]:
        t = self.next_time
        self.next_time = t + self.interval
        self._maybe_random(t)
        active = [e for e in (*self._scripted, *self._active) if e.start <= t < e.end]
        self._active = [e for e in self._active if e.end > t]
        hour = t.timestamp() / 3600 % 24
        rate = self.baseline * self._drift * (1 + self.diurnal * math.sin(2 * math.pi *
                                                                          (hour - 8) / 24))
        for e in active:
            if e.kind in ("spike", "elevation"):
                rate *= e.factor
            elif e.kind == "washout":
                rate += self.baseline * e.factor * math.exp(-(t - e.start).total_seconds() / e.tau)
        days = self.interval.total_seconds() / 86400
        self._drift += (1 - self._drift) * days + self.drift_sigma * math.sqrt(days) * \
            self._rng.gauss(0, 1)
        self._drift = min(1.5, max(0.5, self._drift))
        if any(e.kind == "dropout" for e in active):
            return t, None
        if any(e.kind == "stuck" for e in active) and self._last is not None:
            return t, self._last
        sigma = self.noise * math.sqrt(self.baseline / rate) if rate > 0 else 0
        value = round(max(0.0, rate * (1 + self._rng.gauss(0, sigma))), 4)
        self._last = value
        return t, value


class SimulatedSource:
    """History first (backfill), then live readings in real time (unless realtime=False)."""

    def __init__(self, devices: list[Device], *, backfill_hours: float = 48,
                 realtime: bool = True, seed: int | None = 42, topic_prefix: str = "sim/radiation",
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.devices = [d for d in devices if d.simulation is not None]
        self.backfill = timedelta(hours=backfill_hours)
        self.realtime = realtime
        self.seed = seed
        self.format = SimulatorFormat(topic_prefix)
        self.clock = clock

    async def run(self, emit: Callable[[RawMessage], Awaitable[None]]) -> None:
        anchor = self.clock()
        sims = [DeviceSimulator(d, random.Random(f"{self.seed}:{d.device_id}"
                                                 if self.seed is not None else None),
                                anchor - self.backfill, anchor) for d in self.devices]

        async def emit_step(sim: DeviceSimulator) -> None:
            t, value = sim.step()
            if value is not None:
                await emit(self.format.encode(sim.device.site, sim.device.device_id, sim.metric,
                                              t, value))

        for sim in sims:
            while sim.next_time <= anchor:
                await emit_step(sim)
        if not self.realtime:
            return
        heap = [(s.next_time, i) for i, s in enumerate(sims)]
        heapq.heapify(heap)
        while heap:
            due, i = heap[0]
            wait = (due - self.clock()).total_seconds()
            if wait > 0:
                await asyncio.sleep(min(wait, 1.0))
                continue
            heapq.heappop(heap)
            await emit_step(sims[i])
            heapq.heappush(heap, (sims[i].next_time, i))
