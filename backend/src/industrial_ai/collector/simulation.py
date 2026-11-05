"""Synthetic radiation dose-rate generator for development and testing.

Model per device (values in µSv/h):

    rate(t)  = baseline · drift(t) · (1 + A·sin(2π(t_h − 8)/24)) · Π elevation factors
               + Σ washout bumps
    reading  = max(0, rate · (1 + N(0, σ_rel(rate)))), rounded to the detector resolution
    σ_rel    = counting_noise · sqrt(baseline / rate)    (counting statistics: relative noise
                                                          shrinks as the rate rises)
    drift    = mean-reverting random walk around 1.0 (slow detector/background variation)

Events, either random (rate per day) or scripted at fixed offsets from simulator start:
    spike      single-sample transient (e.g. electronic noise, passing source)
    elevation  plateau of `factor` × rate for `duration_minutes` (e.g. material transfer)
    washout    environmental rain washout: +factor·baseline decaying with τ=duration_minutes
    dropout    no readings (communication loss)
    stuck      the last reading repeats (frozen sensor)

All numbers in scenarios are synthetic. They are not plant data, regulatory limits or
alarm set-points.
"""

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from industrial_ai.schemas.equipment import Equipment

EventType = Literal["spike", "elevation", "washout", "dropout", "stuck"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptedEvent(_Model):
    type: EventType
    # Minutes relative to simulator start ("now"); negative values fall inside the backfill.
    start_offset_minutes: float
    duration_minutes: float = Field(default=0, ge=0)
    factor: float = Field(default=1.0, ge=0)


class RandomEvents(_Model):
    spike_per_day: float = Field(default=0, ge=0)
    spike_factor: tuple[float, float] = (3.0, 8.0)
    elevation_per_day: float = Field(default=0, ge=0)
    elevation_factor: tuple[float, float] = (1.5, 3.0)
    elevation_minutes: tuple[float, float] = (10, 60)
    washout_per_day: float = Field(default=0, ge=0)
    washout_peak: tuple[float, float] = (0.2, 0.6)
    washout_tau_minutes: tuple[float, float] = (60, 180)
    dropout_per_day: float = Field(default=0, ge=0)
    dropout_minutes: tuple[float, float] = (1, 15)
    stuck_per_day: float = Field(default=0, ge=0)
    stuck_minutes: tuple[float, float] = (10, 60)


class DeviceProfile(_Model):
    site_id: str
    equipment_id: str
    metric: str = "dose_rate"
    unit: str = "uSv/h"
    baseline: float = Field(gt=0)
    interval_seconds: int = Field(gt=0)
    counting_noise: float = Field(default=0.05, ge=0)
    diurnal_amplitude: float = Field(default=0.0, ge=0, lt=1)
    drift_sigma_per_day: float = Field(default=0.02, ge=0)
    drift_reversion_per_day: float = Field(default=1.0, ge=0)
    resolution: float = Field(default=0.001, gt=0)
    events: list[ScriptedEvent] = Field(default_factory=list)
    random_events: RandomEvents = Field(default_factory=RandomEvents)


class Scenario(_Model):
    """A scenario file holds the equipment catalog and the simulation for it, so the API's
    EQUIPMENT_CATALOG_PATH and SIMULATOR_SCENARIO_PATH can point at the same file."""

    comment: str | None = Field(default=None, alias="_comment")
    equipment: list[Equipment]
    simulation: "SimulationConfig"

    @model_validator(mode="after")
    def _devices_exist(self) -> "Scenario":
        known = {(e.site_id, e.equipment_id) for e in self.equipment}
        for d in self.simulation.devices:
            if (d.site_id, d.equipment_id) not in known:
                raise ValueError(f"simulated device {d.site_id}/{d.equipment_id} not in equipment")
        return self


class SimulationConfig(_Model):
    devices: list[DeviceProfile] = Field(min_length=1)


Scenario.model_rebuild()
_SCENARIO = TypeAdapter(Scenario)


def load_scenario(path: Path) -> Scenario:
    return _SCENARIO.validate_python(json.loads(path.read_text(encoding="utf-8")))


@dataclass
class _Active:
    kind: EventType
    start: datetime
    end: datetime
    factor: float
    tau_s: float = 0.0


class DeviceSimulator:
    """Steps one device forward in time, one reading per interval."""

    def __init__(self, profile: DeviceProfile, rng: random.Random, *, start: datetime,
                 anchor: datetime) -> None:
        self.profile = profile
        self._rng = rng
        self._dt = timedelta(seconds=profile.interval_seconds)
        # Align to the sampling grid so restarts and multiple devices line up cleanly.
        epoch_s = math.ceil(start.timestamp() / profile.interval_seconds)
        self.next_time = datetime.fromtimestamp(epoch_s * profile.interval_seconds, start.tzinfo)
        self._drift = 1.0
        self._last: float | None = None
        self._active: list[_Active] = []
        self._scripted = sorted(
            (self._activate(e.type, anchor + timedelta(minutes=e.start_offset_minutes),
                            e.duration_minutes, e.factor) for e in profile.events),
            key=lambda a: a.start,
        )

    def _activate(self, kind: EventType, start: datetime, minutes: float, factor: float
                  ) -> _Active:
        if kind == "spike":
            return _Active(kind, start, start + self._dt, factor)
        if kind == "washout":
            tau = minutes * 60
            return _Active(kind, start, start + timedelta(seconds=5 * tau), factor, tau)
        return _Active(kind, start, start + timedelta(minutes=minutes), factor)

    def _maybe_start_random(self, t: datetime) -> None:
        r, rng, per_step = self.profile.random_events, self._rng, self._dt.total_seconds() / 86400

        def roll(per_day: float) -> bool:
            return per_day > 0 and rng.random() < per_day * per_step

        if roll(r.spike_per_day):
            self._active.append(self._activate("spike", t, 0, rng.uniform(*r.spike_factor)))
        if roll(r.elevation_per_day):
            self._active.append(self._activate("elevation", t, rng.uniform(*r.elevation_minutes),
                                               rng.uniform(*r.elevation_factor)))
        if roll(r.washout_per_day):
            self._active.append(self._activate("washout", t, rng.uniform(*r.washout_tau_minutes),
                                               rng.uniform(*r.washout_peak)))
        if roll(r.dropout_per_day):
            self._active.append(self._activate("dropout", t, rng.uniform(*r.dropout_minutes), 1))
        if roll(r.stuck_per_day):
            self._active.append(self._activate("stuck", t, rng.uniform(*r.stuck_minutes), 1))

    def step(self) -> tuple[datetime, float | None]:
        """Return (timestamp, reading); reading is None while the device is silent."""
        p, t = self.profile, self.next_time
        self.next_time = t + self._dt
        self._maybe_start_random(t)
        active = [a for a in (*self._scripted, *self._active) if a.start <= t < a.end]
        self._active = [a for a in self._active if a.end > t]

        hours = t.timestamp() / 3600
        rate = p.baseline * self._drift * (1 + p.diurnal_amplitude *
                                           math.sin(2 * math.pi * (hours % 24 - 8) / 24))
        for a in active:
            if a.kind in ("elevation", "spike"):
                rate *= a.factor
            elif a.kind == "washout":
                rate += p.baseline * a.factor * math.exp(-(t - a.start).total_seconds() / a.tau_s)
        self._advance_drift()

        if any(a.kind == "dropout" for a in active):
            return t, None
        if any(a.kind == "stuck" for a in active) and self._last is not None:
            return t, self._last

        sigma = p.counting_noise * math.sqrt(p.baseline / rate) if rate > 0 else 0.0
        value = max(0.0, rate * (1 + self._rng.gauss(0, sigma)))
        value = round(round(value / p.resolution) * p.resolution, 9)
        self._last = value
        return t, value

    def _advance_drift(self) -> None:
        p = self.profile
        dt_days = self._dt.total_seconds() / 86400
        self._drift += (p.drift_reversion_per_day * (1 - self._drift) * dt_days
                        + p.drift_sigma_per_day * math.sqrt(dt_days) * self._rng.gauss(0, 1))
        self._drift = max(0.5, min(1.5, self._drift))
