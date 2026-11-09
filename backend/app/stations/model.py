"""Data contract for fixed monitoring stations.

Three levels:
  station   id, name, communication address, sampling interval (30 s / 60 s / 5 min),
            calibration factor, dose-rate Level 1 / Level 2 thresholds, battery warning /
            alarm voltages; one battery and one shelter per station
  device    up to 20 per station: instantaneous dose rate (µGy/h), device status, comm status
  station-level series: battery voltage (+ level if reported), AC supply, charging,
            shelter temperature / humidity / door, rainfall (optional)

What goes where:
  IoTDB (time series)   every sample of the measured / reported values below (METRICS)
  openGauss             station configuration and device directory (this model), analysis
                        batches / records / events with a snapshot of the configuration used
  nowhere               mean / standard deviation / completeness (computed from the raw
                        samples at analysis time and stored only inside analysis events)

Statuses and flags are stored in IoTDB as numeric codes (STATUS_CODES, ON / OFF) so that a
single DOUBLE series per metric keeps the history of state changes.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_DEVICES_PER_STATION = 20
SAMPLING_INTERVALS = (30, 60, 300)
STATION_NODE = "_station"  # IoTDB device node holding a station's own series
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$"


class DeviceStatus(StrEnum):
    NORMAL = "normal"
    H1 = "h1"          # Level 1 alarm reported by the device
    H2 = "h2"          # Level 2 alarm reported by the device
    FAULT = "fault"
    OFFLINE = "offline"


STATUS_CODES = {DeviceStatus.NORMAL: 0, DeviceStatus.H1: 1, DeviceStatus.H2: 2,
                DeviceStatus.FAULT: 3, DeviceStatus.OFFLINE: 4}
CODE_STATUS = {v: k for k, v in STATUS_CODES.items()}
ON, OFF = 1.0, 0.0


class Metric(StrEnum):
    # per device
    DOSE_RATE = "dose_rate"          # µGy/h, as reported (unit-normalised, not calibrated)
    STATUS = "status"                # STATUS_CODES
    COMM = "comm"                    # 1 = communication ok, 0 = interrupted
    # per station (IoTDB device node STATION_NODE)
    BATTERY_VOLTAGE = "battery_voltage"  # V
    BATTERY_LEVEL = "battery_level"      # % (only if the station reports it)
    AC_POWER = "ac_power"                # 1 = mains normal, 0 = lost
    CHARGING = "charging"                # 1 = charging, 0 = not charging
    SHELTER_TEMP = "shelter_temp"        # °C
    SHELTER_HUMIDITY = "shelter_humidity"  # %RH
    DOOR = "door"                        # 1 = open, 0 = closed
    RAINFALL = "rainfall"                # mm per sample period (optional)


DEVICE_METRICS = (Metric.DOSE_RATE, Metric.STATUS, Metric.COMM)
STATION_METRICS = (Metric.BATTERY_VOLTAGE, Metric.BATTERY_LEVEL, Metric.AC_POWER,
                   Metric.CHARGING, Metric.SHELTER_TEMP, Metric.SHELTER_HUMIDITY, Metric.DOOR,
                   Metric.RAINFALL)
UNITS = {Metric.DOSE_RATE: "µGy/h", Metric.BATTERY_VOLTAGE: "V", Metric.BATTERY_LEVEL: "%",
         Metric.SHELTER_TEMP: "°C", Metric.SHELTER_HUMIDITY: "%RH", Metric.RAINFALL: "mm"}


class StationDevice(BaseModel):
    device_id: str = Field(pattern=ID_PATTERN)
    name: str
    # False = channel not configured: no records, not part of completeness.
    enabled: bool = True
    location: str | None = None
    simulation: dict[str, Any] | None = Field(default=None, exclude=True)  # simulator only


class Station(BaseModel):
    """One row of station_config (openGauss), seeded from data/stations.json."""

    station_id: str = Field(pattern=ID_PATTERN)
    name: str
    comm_address: str | None = None
    sampling_interval_s: int = 60
    calibration_factor: float = Field(default=1.0, gt=0)
    # Dose-rate thresholds (µGy/h). Both required for dose-rate analysis.
    dose_l1: float | None = Field(default=None, gt=0)
    dose_l2: float | None = Field(default=None, gt=0)
    # Battery thresholds (V). Both required for battery analysis.
    battery_warn_v: float | None = Field(default=None, gt=0)
    battery_alarm_v: float | None = Field(default=None, gt=0)
    # Optional battery trend parameters; rule skipped (and reported) when missing.
    battery_drop_v: float | None = Field(default=None, gt=0)       # drop that counts as abnormal
    battery_drop_minutes: int | None = Field(default=None, gt=0)   # ... within this many minutes
    battery_charge_minutes: int | None = Field(default=None, gt=0)  # expected recharge time
    has_rain_gauge: bool = False
    enabled: bool = True  # False = station not analysed (and not listed as a gap)
    devices: list[StationDevice] = Field(default_factory=list)
    simulation: dict[str, Any] | None = Field(default=None, exclude=True)  # simulator only

    @field_validator("sampling_interval_s")
    @classmethod
    def _interval(cls, v: int) -> int:
        if v not in SAMPLING_INTERVALS:
            raise ValueError(f"sampling_interval_s must be one of {SAMPLING_INTERVALS}")
        return v

    @model_validator(mode="after")
    def _check(self) -> "Station":
        if len(self.devices) > MAX_DEVICES_PER_STATION:
            raise ValueError(f"{self.station_id}: at most {MAX_DEVICES_PER_STATION} devices")
        ids = [d.device_id for d in self.devices]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.station_id}: duplicate device_id")
        if STATION_NODE in ids:
            raise ValueError(f"{self.station_id}: device_id {STATION_NODE} is reserved")
        if self.dose_l1 and self.dose_l2 and self.dose_l1 >= self.dose_l2:
            raise ValueError(f"{self.station_id}: dose_l1 must be below dose_l2")
        if self.battery_warn_v and self.battery_alarm_v and \
                self.battery_alarm_v >= self.battery_warn_v:
            raise ValueError(f"{self.station_id}: battery_alarm_v must be below battery_warn_v")
        return self

    @property
    def dose_configured(self) -> bool:
        return self.dose_l1 is not None and self.dose_l2 is not None

    @property
    def battery_configured(self) -> bool:
        return self.battery_warn_v is not None and self.battery_alarm_v is not None

    def device(self, device_id: str) -> StationDevice | None:
        return next((d for d in self.devices if d.device_id == device_id), None)

    def active_devices(self) -> list[StationDevice]:
        return [d for d in self.devices if d.enabled]

    def series(self) -> list[tuple[str, str, str]]:
        """Every IoTDB series of this station (site, device node, metric)."""
        out = [(self.station_id, d.device_id, m.value) for d in self.devices
               for m in DEVICE_METRICS]
        return out + [(self.station_id, STATION_NODE, m.value) for m in STATION_METRICS]
