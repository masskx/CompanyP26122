"""Data contract for fixed monitoring stations (fields from the workstation prototype,
docs/原型/打开工作站.html: real-time monitoring, station detail, system management).

A station (site) has:
  * up to 20 gamma detectors (equipment type `detector`, e.g. AS01-DET-01)
  * one backup-power unit (`backup_power`, default <site>-BAT-01)
  * one shelter / environment unit (`environment`, default <site>-ENV-01)
Every sample arrives over MQTT (topic maintenance/v1/{site_id}/{equipment_id}/telemetry) and
is stored in IoTDB at <IOTDB_ROOT>.<site_id>.<equipment_id>.<metric>.

What goes where:
  IoTDB (time series)   every received sample of the METRICS below
  station file          the station directory and thresholds (STATIONS_FILE, a versioned
                        snapshot of the main server's configuration; not in openGauss)
  openGauss             only conversations and the copy of work orders (see app/db)
Statuses and flags are numeric codes (STATUS_CODES, ON / OFF) so every metric is one DOUBLE
series.
"""

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_DEVICES_PER_STATION = 20
SAMPLING_INTERVALS = (30, 60, 300)
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


class EquipmentType(StrEnum):
    DETECTOR = "detector"
    BACKUP_POWER = "backup_power"
    ENVIRONMENT = "environment"


class Metric(StrEnum):
    # detector
    DOSE_RATE = "dose_rate"            # γ instantaneous value, µGy/h (as reported)
    DOSE_RATE_AVG = "dose_rate_avg"    # γ average reported by the station, µGy/h
    DOSE_RATE_STD = "dose_rate_std"    # γ standard deviation reported by the station
    STATUS = "status"                  # STATUS_CODES
    COMM = "comm"                      # 1 = communication ok, 0 = interrupted
    # backup power
    BATTERY_VOLTAGE = "battery_voltage"  # V
    BATTERY_LEVEL = "battery_level"      # % (if reported)
    AC_POWER = "ac_power"                # 1 = mains (市电), 0 = on battery (电池)
    CHARGING = "charging"                # 1 = charging
    RUNTIME_HOURS = "runtime_hours"      # reference runtime estimated by the station (if any)
    # environment / shelter
    SHELTER_TEMP = "shelter_temp"        # °C
    SHELTER_HUMIDITY = "shelter_humidity"  # %RH
    DOOR = "door"                        # 1 = open
    RAINFALL = "rainfall"                # mm per sample period (if a rain gauge exists)


METRICS_BY_TYPE: dict[EquipmentType, tuple[Metric, ...]] = {
    EquipmentType.DETECTOR: (Metric.DOSE_RATE, Metric.DOSE_RATE_AVG, Metric.DOSE_RATE_STD,
                             Metric.STATUS, Metric.COMM),
    EquipmentType.BACKUP_POWER: (Metric.BATTERY_VOLTAGE, Metric.BATTERY_LEVEL, Metric.AC_POWER,
                                 Metric.CHARGING, Metric.RUNTIME_HOURS),
    EquipmentType.ENVIRONMENT: (Metric.SHELTER_TEMP, Metric.SHELTER_HUMIDITY, Metric.DOOR,
                                Metric.RAINFALL),
}
METRIC_TYPE = {m: t for t, ms in METRICS_BY_TYPE.items() for m in ms}
UNITS = {Metric.DOSE_RATE: "µGy/h", Metric.DOSE_RATE_AVG: "µGy/h", Metric.DOSE_RATE_STD: "µGy/h",
         Metric.BATTERY_VOLTAGE: "V", Metric.BATTERY_LEVEL: "%", Metric.RUNTIME_HOURS: "h",
         Metric.SHELTER_TEMP: "°C", Metric.SHELTER_HUMIDITY: "%RH", Metric.RAINFALL: "mm"}
METRIC_LABELS = {Metric.DOSE_RATE: "γ瞬时值", Metric.DOSE_RATE_AVG: "γ平均值",
                 Metric.DOSE_RATE_STD: "γ标准差", Metric.STATUS: "设备状态",
                 Metric.COMM: "通信状态", Metric.BATTERY_VOLTAGE: "电池电压",
                 Metric.BATTERY_LEVEL: "电池电量", Metric.AC_POWER: "外部交流供电",
                 Metric.CHARGING: "充电状态", Metric.RUNTIME_HOURS: "参考续航",
                 Metric.SHELTER_TEMP: "方舱温度", Metric.SHELTER_HUMIDITY: "方舱湿度",
                 Metric.DOOR: "方舱门", Metric.RAINFALL: "雨量"}


class StationDevice(BaseModel):
    """A gamma detector."""

    device_id: str = Field(pattern=ID_PATTERN)
    name: str
    enabled: bool = True     # False = channel not configured (no analysis, no completeness)
    serial_no: str | None = None                 # 设备出厂编码
    calibration_valid_until: date | None = None  # 校准有效期至
    simulation: dict[str, Any] | None = Field(default=None, exclude=True)  # simulator only


class Station(BaseModel):
    station_id: str = Field(pattern=ID_PATTERN)
    name: str                                    # 站点名称, e.g. 三废区
    station_type: str | None = None              # 站点类型
    comm_type: Literal["fiber", "4g", "other"] | None = None  # 通信方式 光纤 / 4G
    comm_id: str | None = None                   # 通讯编号
    latitude: float | None = None
    longitude: float | None = None
    sampling_interval_s: int = 60                # 采样周期 30 s / 60 s / 5 min
    calibration_factor: float = Field(default=1.0, gt=0)
    background_dose: float | None = Field(default=None, gt=0)  # 本底值 µGy/h
    # Dose-rate thresholds (µGy/h); both required for dose-rate analysis.
    dose_l1: float | None = Field(default=None, gt=0)
    dose_l2: float | None = Field(default=None, gt=0)
    # Battery thresholds (V); both required for battery analysis.
    battery_warn_v: float | None = Field(default=None, gt=0)
    battery_alarm_v: float | None = Field(default=None, gt=0)
    battery_drop_v: float | None = Field(default=None, gt=0)
    battery_drop_minutes: int | None = Field(default=None, gt=0)
    battery_charge_minutes: int | None = Field(default=None, gt=0)
    battery_runtime_hours: float | None = Field(default=None, gt=0)  # nominal reference runtime
    has_rain_gauge: bool = False
    enabled: bool = True
    backup_power_id: str | None = Field(default=None, pattern=ID_PATTERN)
    environment_id: str | None = Field(default=None, pattern=ID_PATTERN)
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
        self.backup_power_id = self.backup_power_id or f"{self.station_id}-BAT-01"
        self.environment_id = self.environment_id or f"{self.station_id}-ENV-01"
        if len(self.devices) > MAX_DEVICES_PER_STATION:
            raise ValueError(f"{self.station_id}: at most {MAX_DEVICES_PER_STATION} devices")
        ids = [d.device_id for d in self.devices] + [self.backup_power_id, self.environment_id]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.station_id}: duplicate equipment id")
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

    def equipment_type(self, equipment_id: str) -> EquipmentType | None:
        if equipment_id == self.backup_power_id:
            return EquipmentType.BACKUP_POWER
        if equipment_id == self.environment_id:
            return EquipmentType.ENVIRONMENT
        return EquipmentType.DETECTOR if self.device(equipment_id) else None

    def node_for(self, metric: Metric, device_id: str | None = None) -> str:
        """IoTDB device node of a metric: detector metrics live under the detector, the
        others under the station's backup-power or environment unit."""
        kind = METRIC_TYPE[metric]
        if kind is EquipmentType.DETECTOR:
            if device_id is None:
                raise ValueError(f"{metric} needs a detector id")
            return device_id
        return self.backup_power_id if kind is EquipmentType.BACKUP_POWER \
            else self.environment_id  # type: ignore[return-value]

    def equipment_ids(self) -> list[str]:
        return [d.device_id for d in self.devices] + [self.backup_power_id,  # type: ignore[list-item]
                                                      self.environment_id]

    def series(self) -> list[tuple[str, str, str]]:
        """Every IoTDB series of this station (site, equipment, metric)."""
        out = [(self.station_id, d.device_id, m.value) for d in self.devices
               for m in METRICS_BY_TYPE[EquipmentType.DETECTOR]]
        out += [(self.station_id, self.backup_power_id, m.value)  # type: ignore[misc]
                for m in METRICS_BY_TYPE[EquipmentType.BACKUP_POWER]]
        return out + [(self.station_id, self.environment_id, m.value)  # type: ignore[misc]
                      for m in METRICS_BY_TYPE[EquipmentType.ENVIRONMENT]]
