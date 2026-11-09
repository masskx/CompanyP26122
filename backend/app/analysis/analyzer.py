"""One station, one analysis window -> records (with events), configuration gaps.

Reads raw samples from the time-series store, applies app/analysis/rules.py and builds the
statistics, comparisons and context of every event. A failed query raises (the batch
records the error and marks itself partial); it never turns into "no data".
"""

import asyncio
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.analysis import rules
from app.analysis.models import (
    Category,
    Comparison,
    EquipmentType,
    Event,
    Level,
    Record,
    Source,
    worst,
)
from app.analysis.rules import Sample, Span
from app.config import Settings
from app.stations.model import (
    CODE_STATUS,
    STATION_NODE,
    STATUS_CODES,
    UNITS,
    DeviceStatus,
    Metric,
    Station,
    StationDevice,
)
from app.timeseries.iotdb import TimeSeriesStore

BATTERY_ID = "BATTERY"
H1, H2 = STATUS_CODES[DeviceStatus.H1], STATUS_CODES[DeviceStatus.H2]
FAULT, OFFLINE = STATUS_CODES[DeviceStatus.FAULT], STATUS_CODES[DeviceStatus.OFFLINE]


@dataclass(frozen=True)
class Params:
    consecutive: int = 3
    gap_intervals: float = 3
    stuck_minutes: int = 60
    min_completeness: float = 0.9
    fault_level: Level = Level.INSPECT
    charge_factor: float = 1.5
    max_values: int = 3000

    @classmethod
    def from_settings(cls, s: Settings) -> "Params":
        return cls(s.analysis_consecutive_samples, s.analysis_gap_intervals,
                   s.analysis_stuck_minutes, s.analysis_min_completeness,
                   Level(s.analysis_fault_level), s.analysis_charge_factor,
                   s.analysis_max_event_values)


@dataclass
class StationResult:
    station_id: str
    records: list[Record] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    devices_analysed: int = 0


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _r(v: float | None, nd: int = 4) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, nd)


def _rule(rule_id: str, description: str, **params) -> dict:
    return {"id": rule_id, "description": description,
            **{k: v for k, v in params.items() if v is not None}}


class StationAnalyzer:
    def __init__(self, store: TimeSeriesStore, params: Params, tz: ZoneInfo) -> None:
        self.store, self.p, self.tz = store, params, tz
        self._sem = asyncio.Semaphore(8)

    # ---- data access --------------------------------------------------------------------

    async def _series(self, station: str, node: str, metric: Metric, start: datetime,
                      end: datetime) -> list[Sample]:
        async with self._sem:
            points = await self.store.query(station, node, metric.value, start, end)
        return [Sample(p.time, p.value) for p in points
                if p.value is not None and start <= p.time < end]

    # ---- entry point --------------------------------------------------------------------

    async def analyze(self, station: Station, batch_id: str, day: date, start: datetime,
                      end: datetime) -> StationResult:
        res = StationResult(station.station_id)
        st = station.station_id
        if not station.dose_configured:
            res.gaps.append({"station_id": st, "scope": "dose_thresholds",
                             "message": "未配置剂量率一级/二级阈值：不生成该站设备记录"})
        if not station.battery_configured:
            res.gaps.append({"station_id": st, "scope": "battery_thresholds",
                             "message": "未配置蓄电池预警/报警电压：不生成该站备电记录"})
        elif not (station.battery_drop_v and station.battery_drop_minutes):
            res.gaps.append({"station_id": st, "scope": "battery_drop",
                             "message": "未配置市电正常时电压下降判定参数：跳过该规则"})
        if station.battery_configured and not station.battery_charge_minutes:
            res.gaps.append({"station_id": st, "scope": "battery_charge",
                             "message": "未配置预期充电时长：跳过充电时间过长判定"})
        if not station.has_rain_gauge:
            res.gaps.append({"station_id": st, "scope": "rainfall", "severity": "info",
                             "message": "无雨量数据：降雨仅作为可选上下文，记为不可用"})
        if not station.dose_configured and not station.battery_configured:
            return res
        try:
            ctx = await self._station_context(station, start, end)
        except Exception as exc:
            res.errors.append({"station_id": st, "device_id": None,
                               "error": f"{type(exc).__name__}: {exc}"[:500]})
            return res
        jobs: list[tuple[str, asyncio.Future | object]] = []
        if station.dose_configured:
            for d in station.active_devices():
                jobs.append((d.device_id, self._device(station, d, ctx, batch_id, day,
                                                       start, end)))
        if station.battery_configured:
            jobs.append((BATTERY_ID, self._battery(station, ctx, batch_id, day, start, end)))
        results = await asyncio.gather(*(j for _, j in jobs), return_exceptions=True)
        for (device_id, _), out in zip(jobs, results, strict=True):
            if isinstance(out, BaseException):
                res.errors.append({"station_id": st, "device_id": device_id,
                                   "error": f"{type(out).__name__}: {out}"[:500]})
                continue
            res.devices_analysed += 1
            if out is not None:
                res.records.append(out)
        return res

    # ---- station context ----------------------------------------------------------------

    async def _station_context(self, station: Station, start: datetime, end: datetime
                               ) -> dict[Metric, list[Sample]]:
        metrics = [Metric.AC_POWER, Metric.CHARGING, Metric.BATTERY_VOLTAGE,
                   Metric.BATTERY_LEVEL, Metric.SHELTER_TEMP, Metric.SHELTER_HUMIDITY,
                   Metric.DOOR, Metric.RAINFALL]
        values = await asyncio.gather(*(self._series(station.station_id, STATION_NODE, m,
                                                     start, end) for m in metrics))
        return dict(zip(metrics, values, strict=True))

    def _context(self, station: Station, ctx: dict[Metric, list[Sample]], span: Span) -> dict:
        a, b = span.start, span.end
        if a == b:  # single instant: look at the surrounding sampling period
            a -= timedelta(seconds=station.sampling_interval_s)
            b += timedelta(seconds=station.sampling_interval_s)

        def inside(m: Metric) -> list[float]:
            return [s.v for s in ctx[m] if a <= s.t <= b]

        ac = inside(Metric.AC_POWER)
        out: dict = {}
        if not ac:
            out["ac_power"] = {"status": "unavailable", "reason": "该时段无市电状态数据"}
        elif all(v == 1 for v in ac):
            out["ac_power"] = {"status": "normal"}
        else:
            lost = sum(1 for v in ac if v == 0)
            out["ac_power"] = {"status": "lost" if lost == len(ac) else "interrupted",
                               "lost_minutes": _r(lost * station.sampling_interval_s / 60, 1)}
        rain = inside(Metric.RAINFALL)
        if not station.has_rain_gauge:
            out["rainfall"] = {"status": "unavailable", "reason": "该站无雨量计"}
        elif not rain:
            out["rainfall"] = {"status": "unavailable", "reason": "该时段无雨量数据"}
        else:
            out["rainfall"] = {"status": "available", "total_mm": _r(sum(rain), 2),
                               "raining": any(v > 0 for v in rain)}
        temp, hum, door = inside(Metric.SHELTER_TEMP), inside(Metric.SHELTER_HUMIDITY), \
            inside(Metric.DOOR)
        if not (temp or hum or door):
            out["shelter"] = {"status": "unavailable", "reason": "该时段无方舱环境数据"}
        else:
            out["shelter"] = {"status": "available",
                              "temperature_mean": _r(sum(temp) / len(temp), 1) if temp else None,
                              "humidity_mean": _r(sum(hum) / len(hum), 1) if hum else None,
                              "door_opened": any(v == 1 for v in door) if door else None}
        return out

    # ---- comparisons ----------------------------------------------------------------------

    def _shift(self, t: datetime, *, days: int = 0, months: int = 0, years: int = 0
               ) -> datetime | None:
        local = t.astimezone(self.tz)
        try:
            if days:
                d = local.date() - timedelta(days=days)
            else:
                m = local.month - months
                y = local.year - years + (m - 1) // 12
                d = local.date().replace(year=y, month=(m - 1) % 12 + 1)
        except ValueError:
            return None  # e.g. 31 March -> no 31 February
        return datetime.combine(d, local.time(), self.tz).astimezone(UTC)

    async def _ref(self, station: Station, node: str, metric: Metric, scale: float,
                   ref_start: datetime, duration: timedelta) -> tuple[int, int, dict]:
        step = max(1, math.ceil(duration.total_seconds()) + 1)
        async with self._sem:
            buckets = await self.store.aggregate(station.station_id, node, metric.value,
                                                 ref_start, ref_start + timedelta(seconds=step),
                                                 step)
        b = buckets[0] if buckets else None
        expected = rules.expected_samples(ref_start, ref_start + duration,
                                          station.sampling_interval_s)
        if b is None or not b.count:
            return 0, expected, {}
        return b.count, expected, {"mean": b.avg * scale if b.avg is not None else None,
                                   "min": b.min * scale if b.min is not None else None,
                                   "max": b.max * scale if b.max is not None else None}

    async def _comparisons(self, station: Station, node: str, metric: Metric, scale: float,
                           span: Span, mean: float | None) -> dict[str, Comparison]:
        duration = max(span.end - span.start, timedelta(seconds=station.sampling_interval_s))

        def result(ref_start: datetime, count: int, expected: int, agg: dict,
                   days: int | None = None) -> Comparison:
            ref_end = ref_start + duration
            if not count:
                return Comparison(status="unavailable", reason="该参考时段无历史数据",
                                  reference_start=ref_start, reference_end=ref_end)
            if count / expected < 0.5:
                return Comparison(status="unavailable", reference_start=ref_start,
                                  reference_end=ref_end, samples=count,
                                  reason=f"参考时段数据完整率不足（{count}/{expected}）")
            delta = None if mean is None or agg["mean"] is None else mean - agg["mean"]
            return Comparison(status="available", reference_start=ref_start,
                              reference_end=ref_end, samples=count, mean=_r(agg["mean"]),
                              min=_r(agg["min"]), max=_r(agg["max"]), delta_mean=_r(delta),
                              delta_pct=_r(delta / agg["mean"] * 100, 1)
                              if delta is not None and agg["mean"] else None,
                              days_available=days)

        out: dict[str, Comparison] = {}
        for name, shift in (("previous_day", {"days": 1}),
                            ("previous_month_same_day", {"months": 1}),
                            ("previous_year_same_day", {"years": 1})):
            ref_start = self._shift(span.start, **shift)
            if ref_start is None:
                out[name] = Comparison(status="unavailable", reason="参考日期不存在（如 2 月 30 日）")
                continue
            count, expected, agg = await self._ref(station, node, metric, scale, ref_start,
                                                   duration)
            out[name] = result(ref_start, count, expected, agg)
        refs = [self._shift(span.start, days=d) for d in range(1, 8)]
        parts = [(r, *(await self._ref(station, node, metric, scale, r, duration)))
                 for r in refs if r is not None]
        usable = [(r, c, e, a) for r, c, e, a in parts if c and c / e >= 0.5]
        if len(usable) < 4:
            out["previous_7_days"] = Comparison(
                status="unavailable", days_available=len(usable),
                reason=f"前 7 日同时段仅 {len(usable)} 天有可用数据（至少需要 4 天）")
        else:
            n = sum(c for _, c, _, _ in usable)
            agg = {"mean": sum(a["mean"] * c for _, c, _, a in usable) / n,
                   "min": min(a["min"] for _, _, _, a in usable),
                   "max": max(a["max"] for _, _, _, a in usable)}
            c7 = result(usable[-1][0], n, sum(e for _, _, e, _ in usable), agg, len(usable))
            c7.reference_start, c7.reference_end = None, None
            out["previous_7_days"] = c7
        return out

    @staticmethod
    def _not_applicable(reason: str) -> dict[str, Comparison]:
        return {k: Comparison(status="not_applicable", reason=reason) for k in
                ("previous_day", "previous_7_days", "previous_month_same_day",
                 "previous_year_same_day")}

    # ---- event construction ---------------------------------------------------------------

    def _event(self, record_id: str, station: Station, span: Span, *, kind: str, title: str,
               level: Level, source: Source, category: Category, rule: dict,
               unit: str | None = None, values: list[Sample] | None = None,
               expected: int | None = None) -> Event:
        vals = values if values is not None else span.samples
        st = rules.stats([s.v for s in vals])
        if expected is None:
            expected = rules.expected_samples(span.start, span.end, station.sampling_interval_s)
        limit = self.p.max_values
        return Event(event_id=_new_id("evt"), record_id=record_id, code="", kind=kind,
                     title=title, level=level, source=source, category=category,
                     start=span.start, end=span.end, duration_s=span.duration_s,
                     open_at_start=span.open_at_start, open_at_end=span.open_at_end,
                     sample_count=st["count"], expected_samples=expected,
                     completeness=_r(min(1.0, st["count"] / expected), 4) if expected else None,
                     unit=unit, min=_r(st["min"]), max=_r(st["max"]), mean=_r(st["mean"]),
                     std=_r(st["std"], 6), rule=rule,
                     values=[(s.t, round(s.v, 6)) for s in vals[:limit]],
                     values_truncated=len(vals) > limit)

    def _missing_events(self, record_id: str, station: Station, spans: list[Span],
                        offline_status: list[datetime], what: str,
                        ctx: dict[Metric, list[Sample]]) -> list[Event]:
        interval = station.sampling_interval_s
        gap_s = interval * self.p.gap_intervals
        out = []
        for sp in spans:
            if sp.detail.get("whole_window"):
                kind, title, source = "no_data", f"分析窗口内无{what}数据", Source.DATA_QUALITY
                if sp.detail.get("offline_reports"):
                    title += "（期间仅收到离线/通信中断状态）"
                rule = _rule("data.no_data", "分析窗口内没有任何可用样本")
            elif sp.detail.get("offline_reports"):
                offline = any(sp.start <= t <= sp.end for t in offline_status)
                kind = "offline" if offline else "comm_interruption"
                title = "设备离线" if offline else "通信中断"
                source = Source.SOURCE_REPORTED
                rule = _rule(f"data.{kind}", "数据源上报离线状态或通信中断，期间无可用样本")
            else:
                kind, title, source = "sampling_gap", "采样中断", Source.DATA_QUALITY
                rule = _rule("data.sampling_gap", "相邻可用样本间隔超过采样周期的 N 倍",
                             gap_limit_s=gap_s, sampling_interval_s=interval)
            missing = max(1, round(sp.duration_s / interval) - (0 if sp.open_at_start or
                                                                 sp.open_at_end else 1))
            ev = self._event(record_id, station, sp, kind=kind, title=title,
                             level=Level.INSUFFICIENT_DATA, source=source,
                             category=Category.DATA_QUALITY, rule=rule, values=[],
                             expected=missing)
            ev.completeness = 0.0
            ev.comparisons = self._not_applicable("数据缺失事件不做同比/环比")
            ev.context = self._context(station, ctx, sp)
            out.append(ev)
        return out

    def _record(self, record_id: str, batch_id: str, day: date, station: Station,
                equipment: EquipmentType, device_id: str, device_name: str,
                events: list[Event], day_stats: dict, data_status: str) -> Record | None:
        if not events:
            return None
        events.sort(key=lambda e: (e.start, -len(e.kind)))
        for i, e in enumerate(events, 1):
            e.code = f"E{i}"
        level = worst([e.level for e in events])
        kinds = Counter(e.title for e in events)
        title = "；".join(f"{t} ×{n}" if n > 1 else t for t, n in kinds.items())
        return Record(record_id=record_id, batch_id=batch_id, analysis_date=day,
                      station_id=station.station_id, station_name=station.name,
                      equipment_type=equipment, device_id=device_id, device_name=device_name,
                      level=level, title=title,
                      sources=sorted({e.source for e in events}), event_count=len(events),
                      data_status=data_status, day_stats=day_stats,
                      created_at=datetime.now(UTC), events=events)

    # ---- detector ---------------------------------------------------------------------------

    async def _device(self, station: Station, device: StationDevice,
                      ctx: dict[Metric, list[Sample]], batch_id: str, day: date,
                      start: datetime, end: datetime) -> Record | None:
        st, dev = station.station_id, device.device_id
        dose, status, comm = await asyncio.gather(
            self._series(st, dev, Metric.DOSE_RATE, start, end),
            self._series(st, dev, Metric.STATUS, start, end),
            self._series(st, dev, Metric.COMM, start, end))
        interval = station.sampling_interval_s
        max_gap = timedelta(seconds=interval * self.p.gap_intervals)
        same_packet = timedelta(seconds=interval / 2)
        factor = station.calibration_factor
        record_id = _new_id("rec")

        offline_status = [s.t for s in status if int(s.v) == OFFLINE]
        comm_down = [s.t for s in comm if s.v == 0]
        down = set(offline_status) | set(comm_down)
        present = sorted({s.t for s in (*dose, *status, *comm)} - down)

        def usable(s: Sample) -> bool:
            if s.t in down:
                return False
            code = rules.state_at(status, s.t, same_packet)
            c = rules.state_at(comm, s.t, same_packet)
            return code not in (FAULT, OFFLINE) and c != 0

        valid = [s for s in dose if usable(s)]
        calibrated = [Sample(s.t, s.v * factor) for s in valid]
        events: list[Event] = []

        missing = rules.missing_spans(present, start, end, max_gap=max_gap,
                                      offline=sorted(down))
        events += self._missing_events(record_id, station, missing, offline_status, "剂量率",
                                       ctx)
        no_data = any(sp.detail.get("whole_window") for sp in missing)

        # dose-rate thresholds (rule) and statuses reported by the device
        unit = UNITS[Metric.DOSE_RATE]
        reported = rules.runs(status, {H1, H2}, max_gap=max_gap, window_start=start,
                              window_end=end)
        th = [station.dose_l1, station.dose_l2]
        spans = rules.threshold_spans(calibrated, th, consecutive=self.p.consecutive,
                                      max_gap=max_gap, window_start=start)
        covered: set[int] = set()
        for sp in spans:
            lvl_code = H1 if sp.level == 1 else H2
            overlapping = [i for i, r in enumerate(reported)
                           if r.start <= sp.end and sp.start <= r.end]
            covered.update(overlapping)
            name = "dose_l1" if sp.level == 1 else "dose_l2"
            ev = self._event(
                record_id, station, sp, kind=f"dose_h{sp.level}",
                title="剂量率连续超一级阈值" if sp.level == 1 else "剂量率连续超二级阈值",
                level=Level.WATCH if sp.level == 1 else Level.INSPECT, source=Source.RULE,
                category=Category.DOSE, unit=unit,
                rule=_rule(f"dose.consecutive_above_{name}",
                           f"连续 {self.p.consecutive} 个有效样本（已乘校准系数）大于"
                           f"{'一' if sp.level == 1 else '二'}级阈值",
                           threshold=th[sp.level - 1], threshold_name=name, comparison=">",
                           consecutive=self.p.consecutive, calibration_factor=factor))
            ev.corroborated_by_source = any(reported[i].level == lvl_code for i in overlapping)
            ev.comparisons = await self._comparisons(station, dev, Metric.DOSE_RATE, factor,
                                                     sp, ev.mean)
            ev.context = self._context(station, ctx, sp)
            events.append(ev)
        for i, r in enumerate(reported):
            if i in covered:
                continue
            values = [s for s in calibrated if r.start <= s.t <= r.end]
            ev = self._event(
                record_id, station, r, kind=f"reported_h{1 if r.level == H1 else 2}",
                title="设备上报一级报警" if r.level == H1 else "设备上报二级报警",
                level=Level.WATCH if r.level == H1 else Level.INSPECT,
                source=Source.SOURCE_REPORTED, category=Category.DOSE, unit=unit,
                values=values, expected=len(r.samples),
                rule=_rule("source.device_status", "设备自身上报的报警状态（本规则未判定超阈值）",
                           reported_status=CODE_STATUS[r.level].value))
            ev.sample_count = len(r.samples)
            ev.completeness = None
            ev.comparisons = await self._comparisons(station, dev, Metric.DOSE_RATE, factor,
                                                     r, ev.mean)
            ev.context = self._context(station, ctx, r)
            events.append(ev)
        for r in rules.runs(status, {FAULT}, max_gap=max_gap, window_start=start,
                            window_end=end):
            values = [Sample(s.t, s.v * factor) for s in dose if r.start <= s.t <= r.end]
            ev = self._event(record_id, station, r, kind="device_fault", title="设备上报故障",
                             level=self.p.fault_level, source=Source.SOURCE_REPORTED,
                             category=Category.DEVICE, unit=unit, values=values,
                             expected=len(r.samples),
                             rule=_rule("source.device_fault", "设备上报故障状态（故障期间读数不参与阈值判定）"))
            ev.sample_count, ev.completeness = len(r.samples), None
            ev.comparisons = self._not_applicable("设备故障事件不做同比/环比")
            ev.context = self._context(station, ctx, r)
            events.append(ev)
        for sp in rules.stuck_spans(valid, min_duration=timedelta(minutes=self.p.stuck_minutes),
                                    max_gap=max_gap):
            values = [Sample(s.t, s.v * factor) for s in sp.samples]
            ev = self._event(record_id, station, sp, kind="stuck_detector",
                             title="读数长时间不变（标准差为零）", level=Level.INSUFFICIENT_DATA,
                             source=Source.DATA_QUALITY, category=Category.DATA_QUALITY,
                             unit=unit, values=values,
                             rule=_rule("data.stuck", "连续相同读数持续时间达到阈值，疑似探测器卡滞",
                                        min_minutes=self.p.stuck_minutes))
            ev.comparisons = self._not_applicable("数据质量事件不做同比/环比")
            ev.context = self._context(station, ctx, sp)
            events.append(ev)

        expected = rules.expected_samples(start, end - timedelta(seconds=interval), interval)
        completeness = min(1.0, len(valid) / expected)
        has_missing = any(e.kind in ("no_data", "offline", "comm_interruption", "sampling_gap")
                          for e in events)
        if completeness < self.p.min_completeness and not has_missing:
            sp = Span(start, end, 0, [], True, True)
            ev = self._event(record_id, station, sp, kind="low_completeness",
                             title="数据完整率不足", level=Level.INSUFFICIENT_DATA,
                             source=Source.DATA_QUALITY, category=Category.DATA_QUALITY,
                             values=[], expected=expected,
                             rule=_rule("data.low_completeness", "全天有效样本占比低于要求",
                                        min_completeness=self.p.min_completeness))
            ev.sample_count, ev.completeness = len(valid), _r(completeness)
            ev.comparisons = self._not_applicable("数据质量事件不做同比/环比")
            events.append(ev)

        st_dose = rules.stats([s.v for s in calibrated])
        status_counts = Counter(CODE_STATUS.get(int(s.v), DeviceStatus.NORMAL).value
                                for s in status)
        day_stats = {"metric": "dose_rate", "unit": unit, "sampling_interval_s": interval,
                     "calibration_factor": factor, "expected_samples": expected,
                     "received_samples": len(dose), "valid_samples": len(valid),
                     "completeness": _r(completeness), "min": _r(st_dose["min"]),
                     "max": _r(st_dose["max"]), "mean": _r(st_dose["mean"]),
                     "std": _r(st_dose["std"], 6), "status_counts": dict(status_counts),
                     "comm_interrupted_samples": len(comm_down)}
        data_status = "missing" if no_data else \
            "partial" if has_missing or completeness < self.p.min_completeness else "complete"
        return self._record(record_id, batch_id, day, station, EquipmentType.DETECTOR, dev,
                            device.name, events, day_stats, data_status)

    # ---- battery ----------------------------------------------------------------------------

    async def _battery(self, station: Station, ctx: dict[Metric, list[Sample]], batch_id: str,
                       day: date, start: datetime, end: datetime) -> Record | None:
        interval = station.sampling_interval_s
        max_gap = timedelta(seconds=interval * self.p.gap_intervals)
        voltage, ac, charging = ctx[Metric.BATTERY_VOLTAGE], ctx[Metric.AC_POWER], \
            ctx[Metric.CHARGING]
        record_id = _new_id("rec")
        unit = UNITS[Metric.BATTERY_VOLTAGE]
        events: list[Event] = []
        unevaluated: list[str] = []

        missing = rules.missing_spans([s.t for s in voltage], start, end, max_gap=max_gap)
        events += self._missing_events(record_id, station, missing, [], "蓄电池电压", ctx)
        no_data = any(sp.detail.get("whole_window") for sp in missing)

        th = [station.battery_warn_v, station.battery_alarm_v]
        for sp in rules.threshold_spans(voltage, th, below=True, consecutive=self.p.consecutive,
                                        max_gap=max_gap, window_start=start):
            name = "battery_warn_v" if sp.level == 1 else "battery_alarm_v"
            ev = self._event(
                record_id, station, sp, kind="battery_low_warn" if sp.level == 1
                else "battery_low_alarm",
                title="蓄电池电压低于预警值" if sp.level == 1 else "蓄电池电压低于报警值",
                level=Level.WATCH if sp.level == 1 else Level.INSPECT, source=Source.RULE,
                category=Category.BATTERY, unit=unit,
                rule=_rule(f"battery.below_{name}",
                           f"连续 {self.p.consecutive} 个样本低于{'预警' if sp.level == 1 else '报警'}电压",
                           threshold=th[sp.level - 1], threshold_name=name, comparison="<",
                           consecutive=self.p.consecutive))
            ev.comparisons = await self._comparisons(station, STATION_NODE,
                                                     Metric.BATTERY_VOLTAGE, 1.0, sp, ev.mean)
            ev.context = self._context(station, ctx, sp)
            events.append(ev)

        if station.battery_drop_v and station.battery_drop_minutes:
            if not ac:
                unevaluated.append("battery_drop: 无市电状态数据")
            for sp in rules.voltage_drop_spans(
                    voltage, ac, charging, drop_v=station.battery_drop_v,
                    window=timedelta(minutes=station.battery_drop_minutes), max_gap=max_gap):
                ev = self._event(
                    record_id, station, sp, kind="battery_drop_on_ac",
                    title="市电正常时蓄电池电压异常下降", level=Level.WATCH, source=Source.RULE,
                    category=Category.BATTERY, unit=unit,
                    rule=_rule("battery.drop_on_ac", "市电正常且未充电时，电压在时间窗内下降达到阈值",
                               drop_v=station.battery_drop_v,
                               window_minutes=station.battery_drop_minutes))
                ev.comparisons = await self._comparisons(station, STATION_NODE,
                                                         Metric.BATTERY_VOLTAGE, 1.0, sp,
                                                         ev.mean)
                ev.context = self._context(station, ctx, sp)
                if ev.max is not None and ev.min is not None:
                    ev.context["voltage_drop_v"] = _r(ev.max - ev.min, 3)
                events.append(ev)

        sessions: list[dict] = []
        if station.battery_charge_minutes:
            if not charging:
                unevaluated.append("slow_charging: 无充电状态数据")
            limit = station.battery_charge_minutes * self.p.charge_factor
            for sp in rules.charge_sessions(ac, charging, end, max_gap=max_gap):
                minutes = sp.duration_s / 60
                sessions.append({"restored_at": sp.start.isoformat(),
                                 "charge_minutes": _r(minutes, 1), **sp.detail,
                                 "open_at_end": sp.open_at_end})
                if not sp.detail.get("started") or minutes <= limit:
                    continue
                sp.samples = [v for v in voltage if sp.start <= v.t <= sp.end]
                ev = self._event(
                    record_id, station, sp, kind="slow_charging",
                    title="市电恢复后充电时间明显变长", level=Level.WATCH, source=Source.RULE,
                    category=Category.BATTERY, unit=unit,
                    rule=_rule("battery.slow_charging",
                               "市电恢复后充电持续时间超过预期充电时长 × 系数",
                               expected_minutes=station.battery_charge_minutes,
                               factor=self.p.charge_factor, limit_minutes=_r(limit, 1)))
                ev.context = self._context(station, ctx, sp)
                ev.context["charge_minutes"] = _r(minutes, 1)
                ev.context["charge_finished"] = bool(sp.detail.get("finished"))
                ev.comparisons = self._not_applicable("历史充电时长基线将在后续版本提供")
                events.append(ev)

        st_v = rules.stats([s.v for s in voltage])
        level = ctx[Metric.BATTERY_LEVEL]
        outages = rules.runs(ac, {0}, max_gap=max_gap, window_start=start, window_end=end)
        expected = rules.expected_samples(start, end - timedelta(seconds=interval), interval)
        completeness = min(1.0, len(voltage) / expected)
        day_stats = {"metric": "battery_voltage", "unit": unit, "sampling_interval_s": interval,
                     "expected_samples": expected, "valid_samples": len(voltage),
                     "completeness": _r(completeness), "min": _r(st_v["min"], 3),
                     "max": _r(st_v["max"], 3), "mean": _r(st_v["mean"], 3),
                     "battery_level_min": _r(min(s.v for s in level), 1) if level else None,
                     "battery_level_reported": bool(level),
                     "ac_outages": [{"start": o.start.isoformat(), "end": o.end.isoformat(),
                                     "minutes": _r(o.duration_s / 60, 1)} for o in outages],
                     "charge_sessions": sessions, "unevaluated_rules": unevaluated}
        has_missing = len(missing) > 0
        data_status = "missing" if no_data else \
            "partial" if has_missing or completeness < self.p.min_completeness else "complete"
        return self._record(record_id, batch_id, day, station, EquipmentType.BACKUP_POWER,
                            BATTERY_ID, "蓄电池（站级备电）", events, day_stats, data_status)
