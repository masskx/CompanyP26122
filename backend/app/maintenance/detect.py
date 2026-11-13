"""Rule layer of the maintenance analysis (no model involved).

For one detector or one backup-power unit and one observation window it produces:
  hits     rule hits (become `findings`): threshold / alarm / fault / stuck / trend /
           correlation / battery rules; every hit traces back to a rule id, metric, observed
           value, baseline, window and source series
  gaps     data-quality problems (become the single `limitation`); `blocking` gaps (key metric
           without usable data, low completeness, missing thresholds) force insufficient_data
  quality  sample count / completeness / data_as_of of the key metric (data_snapshot)

Baselines come from the previous ANALYSIS_BASELINE_DAYS days (前两周), falling back to the
station's configured background (本底值) or nominal battery runtime. A failed time-series
query raises: it is a processing error, never "no data".
"""

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.config import Settings
from app.maintenance import rules
from app.maintenance.rules import Sample
from app.stations.model import STATUS_CODES, DeviceStatus, Metric, Station, StationDevice
from app.timeseries.iotdb import TimeSeriesStore

H1, H2 = STATUS_CODES[DeviceStatus.H1], STATUS_CODES[DeviceStatus.H2]
FAULT, OFFLINE = STATUS_CODES[DeviceStatus.FAULT], STATUS_CODES[DeviceStatus.OFFLINE]

# Order of importance among findings of the same severity.
RULE_PRIORITY = [
    "DETECTOR-CONSECUTIVE-ALARM", "BACKUP-VOLTAGE-ALARM", "DETECTOR-DEVICE-FAULT",
    "BACKUP-RUNTIME-DROP", "DETECTOR-DEVICE-ALARM", "DETECTOR-CONSECUTIVE-WARNING",
    "DETECTOR-ALARM-POINT", "BACKUP-ON-BATTERY", "BACKUP-VOLTAGE-LOW",
    "BACKUP-VOLTAGE-DROP-ON-AC", "BACKUP-SLOW-RECHARGE", "DETECTOR-STUCK-READING",
    "DETECTOR-RAIN-CORRELATION", "DETECTOR-DOSE-RISE", "HISTORY-REPEAT"]


@dataclass
class Hit:
    rule_id: str
    code: str
    severity: str            # watch | inspect
    metric: str
    start: datetime
    end: datetime
    message: str
    observed: float | None = None
    baseline: float | None = None
    source_refs: list[str] = field(default_factory=list)

    @property
    def change_percent(self) -> float | None:
        if self.observed is None or not self.baseline:
            return None
        return round((self.observed - self.baseline) / self.baseline * 100, 1)

    @property
    def rank(self) -> tuple:
        prio = RULE_PRIORITY.index(self.rule_id) if self.rule_id in RULE_PRIORITY else 99
        return (0 if self.severity == "inspect" else 1, prio, -self.end.timestamp())


@dataclass
class Gap:
    code: str
    text: str
    affects: str
    blocking: bool = False


@dataclass
class Quality:
    sample_count: int = 0
    expected: int = 0
    data_as_of: datetime | None = None

    @property
    def completeness(self) -> float:
        return round(min(1.0, self.sample_count / self.expected), 4) if self.expected else 0.0


@dataclass
class Detection:
    equipment_id: str
    hits: list[Hit] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    quality: Quality = field(default_factory=Quality)
    reported_alarm: bool = False   # the device itself reported H1 / H2 / fault in the window


def _r(v: float | None, nd: int = 4) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, nd)


def _hhmm(t: datetime, tz) -> str:
    return t.astimezone(tz).strftime("%m-%d %H:%M")


def window_text(start: datetime, end: datetime, tz) -> str:
    return f"{start.astimezone(tz).isoformat(timespec='seconds')}/" \
           f"{end.astimezone(tz).isoformat(timespec='seconds')}"


class Detector:
    def __init__(self, store: TimeSeriesStore, s: Settings, tz) -> None:
        self.store, self.s, self.tz = store, s, tz

    async def _series(self, site: str, node: str, metric: Metric, start: datetime,
                      end: datetime) -> list[Sample]:
        points = await self.store.query(site, node, metric.value, start, end)
        return [Sample(p.time, p.value) for p in points
                if p.value is not None and start <= p.time <= end]

    async def _baseline(self, site: str, node: str, metric: Metric, start: datetime,
                        scale: float = 1.0) -> float | None:
        """Mean of the previous ANALYSIS_BASELINE_DAYS days (前两周)."""
        days = self.s.analysis_baseline_days
        b_start = start - timedelta(days=days)
        buckets = await self.store.aggregate(site, node, metric.value, b_start, start,
                                             days * 86400)
        b = buckets[0] if buckets else None
        if b is None or not b.count or b.avg is None:
            return None
        return b.avg * scale

    def _ref(self, site: str, node: str, metric: Metric, start: datetime, end: datetime) -> str:
        return f"ts:{site}:{node}:{metric.value}:{_hhmm(start, self.tz)}/{_hhmm(end, self.tz)}"

    # ---- detector ---------------------------------------------------------------------------

    async def detector(self, st: Station, dev: StationDevice, start: datetime, end: datetime
                       ) -> Detection:
        s, site, node = self.s, st.station_id, dev.device_id
        det = Detection(node)
        dose = await self._series(site, node, Metric.DOSE_RATE, start, end)
        status = await self._series(site, node, Metric.STATUS, start, end)
        comm = await self._series(site, node, Metric.COMM, start, end)
        rain = await self._series(site, st.environment_id, Metric.RAINFALL, start, end) \
            if st.has_rain_gauge else []  # type: ignore[arg-type]
        interval = st.sampling_interval_s
        max_gap = timedelta(seconds=interval * s.analysis_gap_intervals)
        same = timedelta(seconds=interval / 2)
        factor = st.calibration_factor

        down = {x.t for x in status if int(x.v) == OFFLINE} | {x.t for x in comm if x.v == 0}

        def usable(x: Sample) -> bool:
            code = rules.state_at(status, x.t, same)
            return x.t not in down and code not in (FAULT, OFFLINE) and \
                rules.state_at(comm, x.t, same) != 0

        valid = [x for x in dose if usable(x)]
        cal = [Sample(x.t, x.v * factor) for x in valid]
        det.quality = Quality(len(valid), rules.expected_samples(start, end, interval) - 1 or 1,
                              max((x.t for x in valid), default=None))
        det.reported_alarm = any(int(x.v) in (H1, H2, FAULT) for x in status)

        # data quality --------------------------------------------------------------------
        present = sorted({x.t for x in (*dose, *status, *comm)} - down)
        missing = rules.missing_spans(present, start, end, max_gap=max_gap,
                                      offline=sorted(down))
        if not valid:
            det.gaps.append(Gap("detector.no_data", f"{node} 窗口内无可用剂量率数据",
                                "无法判断探测器运行状态", blocking=True))
        elif det.quality.completeness < s.analysis_min_completeness:
            det.gaps.append(Gap("detector.low_completeness",
                                f"{node} 剂量率数据完整率 {det.quality.completeness * 100:.1f}%，"
                                f"低于 {s.analysis_min_completeness * 100:.0f}%",
                                "无法给出可靠的剂量率判断", blocking=True))
        offline_min = sum(sp.duration_s for sp in missing
                          if sp.detail.get("offline_reports")) / 60
        gap_min = sum(sp.duration_s for sp in missing
                      if not sp.detail.get("offline_reports")
                      and not sp.detail.get("whole_window")) / 60
        if valid and offline_min:
            det.gaps.append(Gap("detector.offline", f"{node} 离线或通信中断约 "
                                f"{offline_min:.0f} 分钟", "该时段剂量率无法判断"))
        if valid and gap_min:
            det.gaps.append(Gap("detector.data_gap", f"{node} 采样中断约 {gap_min:.0f} 分钟",
                                "该时段剂量率无法判断"))
        if dev.calibration_valid_until and dev.calibration_valid_until < end.astimezone(
                self.tz).date():
            det.gaps.append(Gap("detector.calibration_expired",
                                f"{node} 校准有效期已于 {dev.calibration_valid_until} 到期",
                                "剂量率绝对值可能存在偏差"))
        if not st.dose_configured:
            det.gaps.append(Gap("config.dose_thresholds_missing",
                                f"{site} 未配置剂量率一级/二级阈值",
                                "无法执行阈值规则", blocking=True))
        if not valid or not st.dose_configured:
            return det

        # thresholds ------------------------------------------------------------------------
        baseline = await self._baseline(site, node, Metric.DOSE_RATE, start, factor) \
            or st.background_dose
        th = [st.dose_l1, st.dose_l2]
        n = s.analysis_consecutive_samples
        spans = rules.threshold_spans(cal, th, consecutive=n, max_gap=max_gap,  # type: ignore[arg-type]
                                      window_start=start)
        for sp in spans:
            peak = max(x.v for x in sp.samples)
            lvl = "二级" if sp.level == 2 else "一级"
            det.hits.append(Hit(
                "DETECTOR-CONSECUTIVE-ALARM" if sp.level == 2 else "DETECTOR-CONSECUTIVE-WARNING",
                "detector.dose_rate_alarm" if sp.level == 2 else "detector.dose_rate_warning",
                "inspect" if sp.level == 2 else "watch", "dose_rate", sp.start, sp.end,
                f"{_hhmm(sp.start, self.tz)} 起连续 {len(sp.samples)} 个样本超过{lvl}阈值 "
                f"{th[sp.level - 1]} µGy/h，最高 {peak:.3f} µGy/h",
                _r(peak), _r(baseline), [self._ref(site, node, Metric.DOSE_RATE, sp.start,
                                                   sp.end)]))
        in_alarm = [(sp.start, sp.end) for sp in spans if sp.level == 2]
        points = [x for x in cal if x.v > st.dose_l2  # type: ignore[operator]
                  and not any(a <= x.t <= b for a, b in in_alarm)]
        if points:
            peak = max(x.v for x in points)
            det.hits.append(Hit("DETECTOR-ALARM-POINT", "detector.alarm_point", "watch",
                                "dose_rate", points[0].t, points[-1].t,
                                f"{len(points)} 个样本超过二级阈值 {st.dose_l2} µGy/h（未连续 "
                                f"{n} 个），最高 {peak:.3f} µGy/h", _r(peak), _r(baseline),
                                [self._ref(site, node, Metric.DOSE_RATE, points[0].t,
                                           points[-1].t)]))
        for r in rules.runs(status, {H1, H2}, max_gap=max_gap):
            if any(sp.start <= r.end and r.start <= sp.end for sp in spans):
                continue  # already explained by the threshold rule
            lvl = "二级" if r.level == H2 else "一级"
            det.hits.append(Hit("DETECTOR-DEVICE-ALARM", "detector.device_alarm",
                                "inspect" if r.level == H2 else "watch", "status", r.start, r.end,
                                f"设备自身上报{lvl}报警（{_hhmm(r.start, self.tz)}–"
                                f"{_hhmm(r.end, self.tz)}），本规则未判定连续超阈值",
                                source_refs=[self._ref(site, node, Metric.STATUS, r.start,
                                                       r.end)]))
        for r in rules.runs(status, {FAULT}, max_gap=max_gap):
            minutes = r.duration_s / 60 + interval / 60
            det.hits.append(Hit("DETECTOR-DEVICE-FAULT", "detector.device_fault",
                                s.analysis_fault_level, "status", r.start, r.end,
                                f"设备上报故障，持续约 {minutes:.0f} 分钟"
                                f"（{_hhmm(r.start, self.tz)} 起）", _r(minutes), None,
                                [self._ref(site, node, Metric.STATUS, r.start, r.end)]))
        for sp in rules.stuck_spans(valid, min_duration=timedelta(
                minutes=s.analysis_stuck_minutes), max_gap=max_gap):
            minutes = sp.duration_s / 60
            det.hits.append(Hit("DETECTOR-STUCK-READING", "detector.stuck_reading", "watch",
                                "dose_rate", sp.start, sp.end,
                                f"读数连续 {minutes:.0f} 分钟保持 {sp.samples[0].v * factor:.4f} "
                                "µGy/h 不变（标准差为零）", _r(minutes), None,
                                [self._ref(site, node, Metric.DOSE_RATE, sp.start, sp.end)]))

        # trend / correlation (only when no threshold rule fired) ------------------------------
        if baseline and not any(h.metric == "dose_rate" and h.rule_id.startswith(
                ("DETECTOR-CONSECUTIVE", "DETECTOR-ALARM")) for h in det.hits):
            hourly: dict[datetime, list[float]] = {}
            for x in cal:
                hourly.setdefault(x.t.replace(minute=0, second=0, microsecond=0), []).append(x.v)
            best = max(((h, statistics.fmean(v)) for h, v in hourly.items() if len(v) >= 3),
                       key=lambda kv: kv[1], default=None)
            if best and (best[1] - baseline) / baseline * 100 >= s.analysis_rise_watch_pct:
                h_start, mean = best
                h_end = h_start + timedelta(hours=1)
                wet = [x.v for x in rain if h_start - timedelta(hours=1) <= x.t < h_end]
                rained = sum(wet) > 0
                refs = [self._ref(site, node, Metric.DOSE_RATE, h_start, h_end)]
                if rained:
                    refs.append(self._ref(site, st.environment_id, Metric.RAINFALL,  # type: ignore[arg-type]
                                          h_start - timedelta(hours=1), h_end))
                det.hits.append(Hit(
                    "DETECTOR-RAIN-CORRELATION" if rained else "DETECTOR-DOSE-RISE",
                    "detector.rainfall_correlated_rise" if rained else "detector.dose_rate_rise",
                    "watch", "dose_rate", h_start, h_end,
                    f"{_hhmm(h_start, self.tz)} 小时均值 {mean:.3f} µGy/h，较前两周基线 "
                    f"{baseline:.3f} µGy/h 升高"
                    + (f"，同期降雨 {sum(wet):.1f} mm" if rained else "，同期无降雨记录"),
                    _r(mean), _r(baseline), refs))
        return det

    # ---- backup power -----------------------------------------------------------------------

    async def backup(self, st: Station, start: datetime, end: datetime) -> Detection:
        s, site, node = self.s, st.station_id, st.backup_power_id
        det = Detection(node)  # type: ignore[arg-type]
        voltage = await self._series(site, node, Metric.BATTERY_VOLTAGE, start, end)  # type: ignore[arg-type]
        ac = await self._series(site, node, Metric.AC_POWER, start, end)  # type: ignore[arg-type]
        charging = await self._series(site, node, Metric.CHARGING, start, end)  # type: ignore[arg-type]
        runtime = await self._series(site, node, Metric.RUNTIME_HOURS, start, end)  # type: ignore[arg-type]
        interval = st.sampling_interval_s
        max_gap = timedelta(seconds=interval * s.analysis_gap_intervals)
        det.quality = Quality(len(voltage), rules.expected_samples(start, end, interval) - 1 or 1,
                              max((x.t for x in voltage), default=None))
        if not voltage:
            det.gaps.append(Gap("backup.no_data", f"{node} 窗口内无蓄电池电压数据",
                                "无法判断备电状态", blocking=True))
        elif det.quality.completeness < s.analysis_min_completeness:
            det.gaps.append(Gap("backup.low_completeness",
                                f"{node} 电压数据完整率 {det.quality.completeness * 100:.1f}%",
                                "无法给出可靠的备电判断", blocking=True))
        if not st.battery_configured:
            det.gaps.append(Gap("config.battery_thresholds_missing",
                                f"{site} 未配置蓄电池预警/报警电压", "无法执行电压规则",
                                blocking=True))
        if not voltage or not st.battery_configured:
            return det

        ref = lambda m, a, b: self._ref(site, node, m, a, b)  # noqa: E731
        th = [st.battery_warn_v, st.battery_alarm_v]
        for sp in rules.threshold_spans(voltage, th, below=True,  # type: ignore[arg-type]
                                        consecutive=s.analysis_consecutive_samples,
                                        max_gap=max_gap, window_start=start):
            low = min(x.v for x in sp.samples)
            det.hits.append(Hit(
                "BACKUP-VOLTAGE-ALARM" if sp.level == 2 else "BACKUP-VOLTAGE-LOW",
                "backup.voltage_below_alarm" if sp.level == 2 else "backup.voltage_below_warning",
                "inspect" if sp.level == 2 else "watch", "battery_voltage", sp.start, sp.end,
                f"电压低于{'报警' if sp.level == 2 else '预警'}值 {th[sp.level - 1]} V，"
                f"最低 {low:.2f} V（{_hhmm(sp.start, self.tz)}–{_hhmm(sp.end, self.tz)}）",
                _r(low, 3), th[sp.level - 1],
                [ref(Metric.BATTERY_VOLTAGE, sp.start, sp.end)]))
        if ac and ac[-1].v == 0 and end - ac[-1].t <= max_gap:
            since = ac[-1].t
            for x in reversed(ac):
                if x.v != 0:
                    break
                since = x.t
            hours = (ac[-1].t - since).total_seconds() / 3600
            det.hits.append(Hit("BACKUP-ON-BATTERY", "backup.mains_lost", "watch", "ac_power",
                                since, ac[-1].t,
                                f"外部交流供电中断，已由蓄电池供电约 {hours:.1f} 小时，"
                                f"当前电压 {voltage[-1].v:.2f} V", _r(hours, 1),
                                st.battery_runtime_hours,
                                [ref(Metric.AC_POWER, since, ac[-1].t)]))
        if st.battery_drop_v and st.battery_drop_minutes:
            for sp in rules.voltage_drop_spans(voltage, ac, charging, drop_v=st.battery_drop_v,
                                               window=timedelta(minutes=st.battery_drop_minutes),
                                               max_gap=max_gap):
                drop = max(x.v for x in sp.samples) - min(x.v for x in sp.samples)
                det.hits.append(Hit("BACKUP-VOLTAGE-DROP-ON-AC", "backup.voltage_drop_on_mains",
                                    "watch", "battery_voltage", sp.start, sp.end,
                                    f"市电正常且未充电时电压下降 {drop:.2f} V"
                                    f"（{_hhmm(sp.start, self.tz)}–{_hhmm(sp.end, self.tz)}）",
                                    _r(drop, 3), st.battery_drop_v,
                                    [ref(Metric.BATTERY_VOLTAGE, sp.start, sp.end)]))
        if st.battery_charge_minutes:
            limit = st.battery_charge_minutes * s.analysis_charge_factor
            for sp in rules.charge_sessions(ac, charging, end, max_gap=max_gap):
                minutes = sp.duration_s / 60
                if sp.detail.get("started") and minutes > limit:
                    det.hits.append(Hit("BACKUP-SLOW-RECHARGE", "backup.slow_recharge", "watch",
                                        "charge_minutes", sp.start, sp.end,
                                        f"市电恢复后充电持续 {minutes:.0f} 分钟"
                                        f"{'（仍未结束）' if sp.open_at_end else ''}，预期 "
                                        f"{st.battery_charge_minutes} 分钟", _r(minutes, 1),
                                        float(st.battery_charge_minutes),
                                        [ref(Metric.CHARGING, sp.start, sp.end)]))
            if not charging:
                det.gaps.append(Gap("backup.missing_charge_state", f"{node} 无充电状态数据",
                                    "无法评估充电恢复时间"))
        if runtime:
            observed = runtime[-1].v
            base = await self._baseline(site, node, Metric.RUNTIME_HOURS, start) \
                or st.battery_runtime_hours  # type: ignore[arg-type]
            if base:
                drop_pct = (base - observed) / base * 100
                if drop_pct >= s.analysis_runtime_drop_watch_pct:
                    det.hits.append(Hit(
                        "BACKUP-RUNTIME-DROP", "backup.runtime_drop",
                        "inspect" if drop_pct >= s.analysis_runtime_drop_inspect_pct else "watch",
                        "runtime_hours", runtime[0].t, runtime[-1].t,
                        f"参考续航 {observed:.1f} 小时，较基线 {base:.1f} 小时下降约 "
                        f"{drop_pct:.0f}%", _r(observed, 2), _r(base, 2),
                        [ref(Metric.RUNTIME_HOURS, runtime[0].t, runtime[-1].t)]))
        else:
            det.gaps.append(Gap("backup.missing_runtime", f"{node} 未上报参考续航",
                                "无法评估续航变化"))
        return det
