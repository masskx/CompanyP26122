"""Synthetic fixed-station data (development / demonstration only, NOT plant data).

Publishes the telemetry_v1 contract: one message per equipment unit and sampling instant,
topic maintenance/v1/{site_id}/{equipment_id}/telemetry, with a global, monotonically
increasing source_sequence (what the main server's MQTT publisher provides).

Profiles live in data/stations.json ("simulation" blocks, never used by the analysis).
Scripted events either start `ago_hours` before the simulator starts, or at a local clock
time (`start`, ANALYSIS_TIMEZONE) on a relative `day` (-1 = yesterday, "daily" = every day).

Detector profile:
    {"baseline": 0.085, "noise": 0.04, "rain_effect": 0.02, "events": [...]}
  elevation   dose rate around `level` µGy/h; the device reports H1/H2 against the
              station thresholds unless "report_status": false
  stuck       the last value repeats (frozen detector)
  offline     status "offline", comm "interrupted", no dose rate
  comm_loss   comm "interrupted", no dose rate
  gap         nothing received
  fault       status "fault" (dose rate still sent)
Station profile ("battery", "shelter", "events"):
  ac_loss, slow_charge (factor), battery_drain (v_per_h), rain (mm_per_hour), door_open,
  runtime_drop (runtime_hours reported from then on), station_offline
"""

import asyncio
import heapq
import math
import random
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.ingestion.parsers import RawMessage, TelemetryV1Format
from app.stations.model import Station, StationDevice


@dataclass
class _Span:
    kind: str
    start: datetime
    end: datetime
    params: dict


def _spans(events: list[dict], tz: ZoneInfo, first: date, last: date, anchor: datetime
           ) -> list[_Span]:
    out = []
    for e in events:
        minutes = timedelta(minutes=float(e.get("minutes", 0)))
        if "ago_hours" in e:
            start = anchor - timedelta(hours=float(e["ago_hours"]))
            out.append(_Span(e["type"], start, start + minutes, e))
            continue
        anchor_day = anchor.astimezone(tz).date()
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)] \
            if e.get("day") == "daily" else [anchor_day + timedelta(days=int(e.get("day", -1)))]
        hh, mm = (int(x) for x in str(e.get("start", "00:00")).split(":"))
        for d in days:
            start = datetime.combine(d, time(hh, mm), tz).astimezone(UTC)
            out.append(_Span(e["type"], start, start + minutes, e))
    return out


def _active(spans: list[_Span], t: datetime, kind: str) -> _Span | None:
    return next((s for s in spans if s.kind == kind and s.start <= t < s.end), None)


class _DetectorSim:
    def __init__(self, device: StationDevice, station: Station, spans: list[_Span],
                 rng: random.Random) -> None:
        p = device.simulation or {}
        self.device, self.station, self.spans, self.rng = device, station, spans, rng
        self.baseline = float(p.get("baseline", 0.08))
        self.noise = float(p.get("noise", 0.04))
        self.rain_effect = float(p.get("rain_effect", 0.0))
        self.last: float | None = None
        self.recent: deque[float] = deque(maxlen=10)

    def sample(self, t: datetime, rain_mm_h: float) -> dict | None:
        if _active(self.spans, t, "gap"):
            return None
        if _active(self.spans, t, "offline"):
            return {"status": "offline", "comm": "interrupted"}
        if _active(self.spans, t, "comm_loss"):
            return {"comm": "interrupted"}
        elevation = _active(self.spans, t, "elevation")
        rate = float(elevation.params["level"]) if elevation else \
            self.baseline + self.rain_effect * rain_mm_h
        value = round(max(0.0, rate * (1 + self.rng.gauss(0, self.noise))), 4)
        if _active(self.spans, t, "stuck") and self.last is not None:
            value = self.last
        self.last = value
        self.recent.append(value)
        mean = sum(self.recent) / len(self.recent)
        std = math.sqrt(sum((v - mean) ** 2 for v in self.recent) / len(self.recent))
        status = "normal"
        s = self.station
        if elevation is None or elevation.params.get("report_status", True):
            if s.dose_l2 is not None and value * s.calibration_factor > s.dose_l2:
                status = "h2"
            elif s.dose_l1 is not None and value * s.calibration_factor > s.dose_l1:
                status = "h1"
        if _active(self.spans, t, "fault"):
            status = "fault"
        return {"dose_rate": value, "dose_rate_avg": round(mean, 4),
                "dose_rate_std": round(std, 4), "status": status, "comm": "ok"}


class StationSimulator:
    def __init__(self, station: Station, tz: ZoneInfo, start: datetime, anchor: datetime,
                 end: datetime, seed: int | None) -> None:
        self.station = station
        self.step = timedelta(seconds=station.sampling_interval_s)
        step_s = self.step.total_seconds()
        self.next_time = datetime.fromtimestamp(math.ceil(start.timestamp() / step_s) * step_s,
                                                UTC)
        first, last = start.astimezone(tz).date(), end.astimezone(tz).date()
        p = station.simulation or {}
        self.spans = _spans(p.get("events", []), tz, first, last, anchor)
        self.rng = random.Random(f"{seed}:{station.station_id}" if seed is not None else None)
        self.detectors = [_DetectorSim(d, station, _spans((d.simulation or {}).get("events", []),
                                                          tz, first, last, anchor),
                                       random.Random(f"{seed}:{station.station_id}:"
                                                     f"{d.device_id}" if seed is not None
                                                     else None))
                          for d in station.devices]
        b = p.get("battery", {})
        self.float_v = float(b.get("float_v", 12.15))
        self.rest_v = float(b.get("rest_v", 12.0))
        self.discharge = float(b.get("discharge_v_per_h", 0.12))
        self.charge_minutes = float(b.get("charge_minutes", 120))
        self.reports_level = bool(b.get("reports_level", True))
        self.runtime = float(b.get("runtime_hours", station.battery_runtime_hours or 24))
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
            self.voltage = max(10.5, self.voltage - float(drain.params.get("v_per_h", 0.5)) * dt_h)
            self._charge = None
        else:
            if self._charge is None and self.voltage < self.float_v - 0.1:
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
                    target = self.float_v + 0.6  # absorption voltage
                    self.voltage = from_v + (target - from_v) * min(1.0, progress / 0.6)
            else:
                self.voltage = self.float_v
        self._ac_was_on = ac
        v = round(self.voltage + self.rng.gauss(0, 0.005), 3)
        drop = _active(self.spans, t, "runtime_drop")
        runtime = float(drop.params.get("runtime_hours", self.runtime * 0.75)) if drop \
            else self.runtime
        out: dict = {"battery_voltage": v, "ac_power": "on" if ac else "off",
                     "charging": charging, "runtime_hours": round(runtime, 1)}
        if self.reports_level:
            out["battery_level"] = 100.0 if ac and not charging and not drain else \
                round(max(0.0, min(100.0, (min(v, self.rest_v) - 10.8) / 1.2 * 100)), 1)
        return out

    def messages(self, fmt: TelemetryV1Format) -> tuple[datetime, list[RawMessage]]:
        t = self.next_time
        self.next_time = t + self.step
        st = self.station
        battery = self._battery(t)  # the battery state evolves even while offline
        if _active(self.spans, t, "station_offline"):
            return t, []
        rain = _active(self.spans, t, "rain")
        rain_mm_h = float(rain.params.get("mm_per_hour", 1.0)) if rain else 0.0
        hour = t.timestamp() / 3600 % 24
        env = {"shelter_temp": round(self.temp + 3 * math.sin(2 * math.pi * (hour - 8) / 24)
                                     + self.rng.gauss(0, 0.2), 2),
               "shelter_humidity": round(min(99.0, self.humidity + 12 * (rain_mm_h > 0)
                                             + self.rng.gauss(0, 1)), 1),
               "door": "open" if _active(self.spans, t, "door_open") else "closed"}
        if st.has_rain_gauge:
            env["rainfall"] = round(rain_mm_h * self.step.total_seconds() / 3600, 3)
        out = []
        for d in self.detectors:
            points = d.sample(t, rain_mm_h)
            if points is not None:
                out.append(fmt.encode(st.station_id, d.device.device_id, t, points,
                                      units={"dose_rate": "uGy/h", "dose_rate_avg": "uGy/h",
                                             "dose_rate_std": "uGy/h"}))
        out.append(fmt.encode(st.station_id, st.backup_power_id, t, battery,  # type: ignore[arg-type]
                              units={"battery_voltage": "V", "battery_level": "%",
                                     "runtime_hours": "h"}))
        out.append(fmt.encode(st.station_id, st.environment_id, t, env))  # type: ignore[arg-type]
        return t, out


class SimulatedSource:
    """History first (backfill), then live messages in real time (unless realtime=False)."""

    def __init__(self, stations: list[Station], *, timezone: str = "Asia/Shanghai",
                 backfill_hours: float = 48, realtime: bool = True, seed: int | None = 42,
                 topic_prefix: str = "maintenance/v1", live_days: int = 30,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 first_sequence: int = 1) -> None:
        self.stations = stations
        self.tz = ZoneInfo(timezone)
        self.backfill = timedelta(hours=backfill_hours)
        self.realtime, self.seed, self.live_days = realtime, seed, live_days
        self.format = TelemetryV1Format(topic_prefix)
        self.format._seq = first_sequence - 1
        self.clock = clock

    @property
    def last_sequence(self) -> int:
        return self.format._seq

    async def run(self, emit: Callable[[RawMessage], Awaitable[None]]) -> None:
        anchor = self.clock()
        end = anchor + timedelta(days=self.live_days if self.realtime else 0)
        sims = [StationSimulator(s, self.tz, anchor - self.backfill, anchor, end, self.seed)
                for s in self.stations]

        async def emit_step(sim: StationSimulator) -> None:
            _, messages = sim.messages(self.format)
            for m in messages:
                await emit(m)

        # history in time order across stations, so the sequence follows event time
        heap = [(s.next_time, i) for i, s in enumerate(sims)]
        heapq.heapify(heap)
        while heap and heap[0][0] <= anchor:
            _, i = heapq.heappop(heap)
            await emit_step(sims[i])
            heapq.heappush(heap, (sims[i].next_time, i))
        if not self.realtime:
            return
        while heap:
            due, i = heap[0]
            wait = (due - self.clock()).total_seconds()
            if wait > 0:
                await asyncio.sleep(min(wait, 1.0))
                continue
            heapq.heappop(heap)
            await emit_step(sims[i])
            heapq.heappush(heap, (sims[i].next_time, i))
