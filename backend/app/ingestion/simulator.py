"""Synthetic fixed-station data (development / demonstration only, NOT plant data).

Every station in data/stations.json may carry a "simulation" profile, every device too.
Scripted events use local clock times (ANALYSIS_TIMEZONE) on a day relative to the
simulator's start: "day": -1 is yesterday (the day the 00:30 analysis looks at), 0 today,
"daily" every day.

Device profile:
    {"baseline": 0.085,  µGy/h background      "noise": 0.04,  relative noise
     "rain_effect": 0.02, µGy/h added per mm/h of rain (washout)
     "events": [{"type": "elevation", "day": -1, "start": "02:00", "minutes": 60,
                 "level": 0.30, "report_status": true}, ...]}
  elevation   dose rate around `level` µGy/h; the device reports H1/H2 against the
              station thresholds unless "report_status": false
  stuck       the last value repeats (frozen detector)
  offline     status "offline", comm "interrupted", no dose rate
  comm_loss   comm "interrupted", no dose rate, status not reported
  gap         device missing from the packets (nothing received)
  fault       status "fault" (dose rate still sent, unreliable)
Station profile:
    {"battery": {"float_v": 13.5, "rest_v": 12.8, "discharge_v_per_h": 0.55,
                 "charge_minutes": 120, "reports_level": true},
     "shelter": {"temperature": 22.0, "humidity": 45.0},
     "events": [{"type": "ac_loss", ...}, {"type": "slow_charge", "factor": 2.5, ...},
                {"type": "battery_drain", "v_per_h": 0.8, ...}, {"type": "rain",
                 "mm_per_hour": 2.0, ...}, {"type": "door_open", ...},
                {"type": "station_offline", ...}]}
  slow_charge applies to a recharge that starts while it is active.

`SimulatedSource` emits the same (topic, payload) station packets a broker would deliver.
"""

import asyncio
import heapq
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.ingestion.parsers import RawMessage, SimulatorFormat
from app.stations.model import Station, StationDevice


@dataclass
class _Span:
    kind: str
    start: datetime
    end: datetime
    params: dict


def _spans(events: list[dict], tz: ZoneInfo, first: date, last: date, anchor: date
           ) -> list[_Span]:
    out = []
    for e in events:
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)] \
            if e.get("day") == "daily" else [anchor + timedelta(days=int(e.get("day", -1)))]
        hh, mm = (int(x) for x in str(e.get("start", "00:00")).split(":"))
        for d in days:
            start = datetime.combine(d, time(hh, mm), tz).astimezone(UTC)
            out.append(_Span(e["type"], start, start + timedelta(minutes=float(
                e.get("minutes", 0))), e))
    return out


def _active(spans: list[_Span], t: datetime, kind: str) -> _Span | None:
    return next((s for s in spans if s.kind == kind and s.start <= t < s.end), None)


class _DeviceSim:
    def __init__(self, device: StationDevice, station: Station, spans: list[_Span],
                 rng: random.Random) -> None:
        p = device.simulation or {}
        self.device, self.station, self.spans, self.rng = device, station, spans, rng
        self.baseline = float(p.get("baseline", 0.08))
        self.noise = float(p.get("noise", 0.04))
        self.rain_effect = float(p.get("rain_effect", 0.0))
        self.last: float | None = None

    def sample(self, t: datetime, rain_mm_h: float) -> dict | None:
        if _active(self.spans, t, "gap"):
            return None
        entry: dict = {"id": self.device.device_id}
        if _active(self.spans, t, "offline"):
            return {**entry, "status": "offline", "comm": "interrupted"}
        if _active(self.spans, t, "comm_loss"):
            return {**entry, "comm": "interrupted"}
        elevation = _active(self.spans, t, "elevation")
        rate = float(elevation.params["level"]) if elevation else \
            self.baseline + self.rain_effect * rain_mm_h
        value = round(max(0.0, rate * (1 + self.rng.gauss(0, self.noise))), 4)
        if _active(self.spans, t, "stuck") and self.last is not None:
            value = self.last
        self.last = value
        status = "normal"
        s = self.station
        if elevation is None or elevation.params.get("report_status", True):
            if s.dose_l2 is not None and value * s.calibration_factor > s.dose_l2:
                status = "h2"
            elif s.dose_l1 is not None and value * s.calibration_factor > s.dose_l1:
                status = "h1"
        if _active(self.spans, t, "fault"):
            status = "fault"
        return {**entry, "dose_rate": value, "unit": "uGy/h", "status": status, "comm": "ok"}


class StationSimulator:
    def __init__(self, station: Station, tz: ZoneInfo, start: datetime, anchor: datetime,
                 end: datetime, seed: int | None) -> None:
        self.station = station
        self.step = timedelta(seconds=station.sampling_interval_s)
        step_s = self.step.total_seconds()
        self.next_time = datetime.fromtimestamp(math.ceil(start.timestamp() / step_s) * step_s,
                                                UTC)
        first, last = start.astimezone(tz).date(), end.astimezone(tz).date()
        anchor_day = anchor.astimezone(tz).date()
        p = station.simulation or {}
        self.spans = _spans(p.get("events", []), tz, first, last, anchor_day)
        self.rng = random.Random(f"{seed}:{station.station_id}" if seed is not None else None)
        self.devices = [_DeviceSim(d, station, _spans((d.simulation or {}).get("events", []),
                                                      tz, first, last, anchor_day),
                                   random.Random(f"{seed}:{station.station_id}:{d.device_id}"
                                                 if seed is not None else None))
                        for d in station.devices]
        b = p.get("battery", {})
        self.float_v = float(b.get("float_v", 13.5))
        self.rest_v = float(b.get("rest_v", 12.8))
        self.discharge = float(b.get("discharge_v_per_h", 0.5))
        self.charge_minutes = float(b.get("charge_minutes", 120))
        self.reports_level = bool(b.get("reports_level", True))
        sh = p.get("shelter", {})
        self.temp, self.humidity = float(sh.get("temperature", 22)), float(sh.get("humidity", 45))
        self.voltage = self.float_v
        self._ac_was_on = True
        self._charge: tuple[datetime, datetime, float] | None = None  # start, end, from_v

    def _battery(self, t: datetime) -> dict:
        dt_h = self.step.total_seconds() / 3600
        ac = _active(self.spans, t, "ac_loss") is None
        drain = _active(self.spans, t, "battery_drain")
        charging = False
        if not ac:
            if self._ac_was_on:
                self.voltage = min(self.voltage, self.rest_v)
            self.voltage = max(10.5, self.voltage - self.discharge * dt_h)
            self._charge = None
        elif drain:  # charger fault: voltage falls although mains is present
            self.voltage = max(10.5, self.voltage - float(drain.params.get("v_per_h", 0.8)) * dt_h)
            self._charge = None
        else:
            if self._charge is None and self.voltage < self.float_v - 0.15:
                slow = _active(self.spans, t, "slow_charge")
                minutes = self.charge_minutes * (float(slow.params.get("factor", 2.0))
                                                 if slow else 1.0)
                self._charge = (t, t + timedelta(minutes=minutes), self.voltage)
            if self._charge is not None:
                c_start, c_end, from_v = self._charge
                if t >= c_end:
                    self._charge = None
                    self.voltage = self.float_v
                else:
                    charging = True
                    progress = (t - c_start) / (c_end - c_start)
                    target = self.float_v + 0.9  # absorption voltage
                    self.voltage = from_v + (target - from_v) * min(1.0, progress / 0.6)
            else:
                self.voltage = self.float_v
        self._ac_was_on = ac
        v = round(self.voltage + self.rng.gauss(0, 0.01), 3)
        out: dict = {"voltage": v, "ac_power": "on" if ac else "off", "charging": charging}
        if self.reports_level:
            out["level"] = 100.0 if ac and not charging and not drain else \
                round(max(0.0, min(100.0, (min(v, 12.8) - 11.0) / 1.8 * 100)), 1)
        return out

    def packet(self) -> tuple[datetime, dict | None]:
        t = self.next_time
        self.next_time = t + self.step
        battery = self._battery(t)  # the battery state evolves even while offline
        if _active(self.spans, t, "station_offline"):
            return t, None
        rain = _active(self.spans, t, "rain")
        rain_mm_h = float(rain.params.get("mm_per_hour", 1.0)) if rain else 0.0
        hour = t.timestamp() / 3600 % 24
        shelter = {"temperature": round(self.temp + 3 * math.sin(2 * math.pi * (hour - 8) / 24)
                                        + self.rng.gauss(0, 0.2), 2),
                   "humidity": round(min(99.0, self.humidity + 12 * (rain_mm_h > 0)
                                         + self.rng.gauss(0, 1)), 1),
                   "door": "open" if _active(self.spans, t, "door_open") else "closed"}
        devices = [e for d in self.devices if (e := d.sample(t, rain_mm_h)) is not None]
        rainfall = round(rain_mm_h * self.step.total_seconds() / 3600, 3) \
            if self.station.has_rain_gauge else None
        return t, {"devices": devices, "battery": battery, "shelter": shelter,
                   "rainfall": rainfall}


class SimulatedSource:
    """History first (backfill), then live packets in real time (unless realtime=False)."""

    def __init__(self, stations: list[Station], *, timezone: str = "Asia/Shanghai",
                 backfill_hours: float = 48, realtime: bool = True, seed: int | None = 42,
                 topic_prefix: str = "sim/stations", live_days: int = 30,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.stations = stations
        self.tz = ZoneInfo(timezone)
        self.backfill = timedelta(hours=backfill_hours)
        self.realtime, self.seed, self.live_days = realtime, seed, live_days
        self.format = SimulatorFormat(topic_prefix)
        self.clock = clock

    async def run(self, emit: Callable[[RawMessage], Awaitable[None]]) -> None:
        anchor = self.clock()
        end = anchor + timedelta(days=self.live_days if self.realtime else 0)
        sims = [StationSimulator(s, self.tz, anchor - self.backfill, anchor, end, self.seed)
                for s in self.stations]

        async def emit_step(sim: StationSimulator) -> None:
            t, p = sim.packet()
            if p is not None:
                await emit(self.format.encode(sim.station.station_id, t, **p))

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
