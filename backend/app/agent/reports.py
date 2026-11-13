"""Standard monitoring report (assistant workflow "monitoring_report").

Parameters are fixed: stations, time range, metrics, unit (µGy/h or nGy/h), decimal places and
granularity (hour / day). The Excel file always has three sheets:
  报表说明   parameters, data source, generation time, rules
  监测统计   avg / min / max / sample count per station, metric and period
  数据质量   expected vs received samples, completeness, periods without data
Statistics and missing values come from deterministic code; the model only summarises.
Files are written to REPORTS_DIR (stand-in for object storage); the report record is part of
the user's conversation data (openGauss assistant_report).
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from openpyxl import Workbook
from openpyxl.styles import Font
from pydantic import BaseModel

from app.db.pool import PgPool
from app.stations.model import METRIC_LABELS, UNITS, Metric, Station
from app.timeseries.iotdb import TimeSeriesStore

REPORT_METRICS = (Metric.DOSE_RATE, Metric.DOSE_RATE_AVG, Metric.DOSE_RATE_STD,
                  Metric.BATTERY_VOLTAGE, Metric.SHELTER_TEMP, Metric.SHELTER_HUMIDITY,
                  Metric.RAINFALL)
DOSE_METRICS = (Metric.DOSE_RATE, Metric.DOSE_RATE_AVG, Metric.DOSE_RATE_STD)


class ReportParams(BaseModel):
    site_ids: list[str]
    start: datetime
    end: datetime
    metrics: list[Metric] = [Metric.DOSE_RATE]
    unit: str = "µGy/h"            # dose-rate unit: µGy/h | nGy/h
    decimals: int = 3
    granularity: str = "hour"      # hour | day


class ReportRecord(BaseModel):
    report_id: str
    user_id: str
    session_id: str | None
    title: str
    params: dict
    file_name: str
    size_bytes: int
    row_count: int
    created_at: datetime


@dataclass
class ReportSummary:
    record: ReportRecord
    stats: list[dict]      # per station / metric: mean, min, max, count, completeness
    quality: list[dict]


class ReportStore(Protocol):
    async def add(self, r: ReportRecord) -> None: ...
    async def get(self, user_id: str, report_id: str) -> ReportRecord | None: ...
    async def list(self, user_id: str, limit: int, offset: int) -> list[ReportRecord]: ...
    async def reassign(self, from_user: str, to_user: str) -> int: ...


class MemoryReportStore:
    def __init__(self) -> None:
        self.items: dict[str, ReportRecord] = {}

    async def add(self, r: ReportRecord) -> None:
        self.items[r.report_id] = r

    async def get(self, user_id: str, report_id: str) -> ReportRecord | None:
        r = self.items.get(report_id)
        return r if r and r.user_id == user_id else None

    async def list(self, user_id: str, limit: int, offset: int) -> list[ReportRecord]:
        items = sorted((r for r in self.items.values() if r.user_id == user_id),
                       key=lambda r: r.created_at, reverse=True)
        return items[offset:offset + limit]

    async def reassign(self, from_user: str, to_user: str) -> int:
        moved = [r for r in self.items.values() if r.user_id == from_user]
        for r in moved:
            r.user_id = to_user
        return len(moved)


class PgReportStore:
    _COLS = ("report_id, user_id, session_id, title, params, file_name, size_bytes, row_count, "
             "created_at")

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    @staticmethod
    def _rec(row: dict) -> ReportRecord:
        return ReportRecord(**{**row, "params": json.loads(row["params"])})

    async def add(self, r: ReportRecord) -> None:
        async with await self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO assistant_report ({self._COLS}) VALUES ({', '.join(['%s'] * 9)})",
                (r.report_id, r.user_id, r.session_id, r.title,
                 json.dumps(r.params, ensure_ascii=False, default=str), r.file_name,
                 r.size_bytes, r.row_count, r.created_at))

    async def get(self, user_id: str, report_id: str) -> ReportRecord | None:
        async with await self._pool.connection() as conn:
            row = await (await conn.execute(
                f"SELECT {self._COLS} FROM assistant_report WHERE report_id = %s AND "
                "user_id = %s AND deleted_at IS NULL", (report_id, user_id))).fetchone()
        return self._rec(row) if row else None

    async def list(self, user_id: str, limit: int, offset: int) -> list[ReportRecord]:
        async with await self._pool.connection() as conn:
            rows = await (await conn.execute(
                f"SELECT {self._COLS} FROM assistant_report WHERE user_id = %s AND "
                "deleted_at IS NULL ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (user_id, limit, offset))).fetchall()
        return [self._rec(r) for r in rows]

    async def reassign(self, from_user: str, to_user: str) -> int:
        async with await self._pool.connection() as conn:
            cur = await conn.execute("UPDATE assistant_report SET user_id = %s WHERE user_id = %s",
                                     (to_user, from_user))
            return cur.rowcount


def _scale(metric: Metric, unit: str) -> float:
    return 1000.0 if metric in DOSE_METRICS and unit == "nGy/h" else 1.0


async def build_report(params: ReportParams, stations: list[Station], ts: TimeSeriesStore,
                       out_dir: Path, user_id: str, session_id: str | None, tz) -> ReportSummary:
    step = 3600 if params.granularity == "hour" else 86400
    d = params.decimals

    def rnd(v: float | None, scale: float) -> float | None:
        return None if v is None else round(v * scale, d)

    rows: list[list] = []
    stats: list[dict] = []
    quality: list[dict] = []
    for st in stations:
        for metric in params.metrics:
            nodes = [dev.device_id for dev in st.active_devices()] \
                if metric in DOSE_METRICS else [st.node_for(metric)]
            scale = _scale(metric, params.unit)
            unit = params.unit if metric in DOSE_METRICS else (UNITS.get(metric) or "")
            for node in nodes:
                buckets = await ts.aggregate(st.station_id, node, metric.value, params.start,
                                             params.end, step)
                total = sum(b.count for b in buckets)
                values = [b for b in buckets if b.count]
                for b in buckets:
                    rows.append([st.station_id, st.name, node, METRIC_LABELS[metric], unit,
                                 b.time.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                                 rnd(b.avg, scale), rnd(b.min, scale), rnd(b.max, scale),
                                 b.count])
                expected = int((params.end - params.start).total_seconds()
                               // st.sampling_interval_s)
                mean = sum(b.avg * b.count for b in values) / total if total else None
                stats.append({"station": st.station_id, "equipment": node,
                              "metric": metric.value, "unit": unit,
                              "mean": rnd(mean, scale),
                              "min": rnd(min((b.min for b in values), default=None), scale),
                              "max": rnd(max((b.max for b in values), default=None), scale),
                              "samples": total})
                quality.append({"station": st.station_id, "equipment": node,
                                "metric": metric.value, "expected": expected, "received": total,
                                "completeness": round(min(1.0, total / expected), 4)
                                if expected else 0.0,
                                "empty_periods": sum(1 for b in buckets if not b.count),
                                "periods": len(buckets)})

    report_id = f"rpt_{uuid.uuid4().hex[:16]}"
    start_l, end_l = params.start.astimezone(tz), params.end.astimezone(tz)
    title = f"监测数据统计报表 {start_l:%Y-%m-%d %H:%M} 至 {end_l:%Y-%m-%d %H:%M}"
    file_name = f"monitoring-report-{start_l:%Y%m%d}-{end_l:%Y%m%d}-{report_id[4:12]}.xlsx"
    path = out_dir / f"{report_id}.xlsx"
    size = await asyncio.to_thread(_write_workbook, path, title, params, stations, rows,
                                   quality, tz)
    record = ReportRecord(report_id=report_id, user_id=user_id, session_id=session_id,
                          title=title, params=params.model_dump(mode="json"),
                          file_name=file_name, size_bytes=size,
                          row_count=len(rows), created_at=datetime.now(UTC))
    return ReportSummary(record, stats, quality)


def _write_workbook(path: Path, title: str, params: ReportParams, stations: list[Station],
                    rows: list[list], quality: list[dict], tz) -> int:
    wb = Workbook()
    bold = Font(bold=True)
    info = wb.active
    info.title = "报表说明"
    start_l, end_l = params.start.astimezone(tz), params.end.astimezone(tz)
    for row in (["报表名称", title],
                ["监测站点", "、".join(f"{s.station_id} {s.name}" for s in stations)],
                ["时间范围", f"{start_l:%Y-%m-%d %H:%M} – {end_l:%Y-%m-%d %H:%M}"],
                ["监测指标", "、".join(METRIC_LABELS[m] for m in params.metrics)],
                ["剂量率单位", params.unit], ["保留小数位", params.decimals],
                ["统计口径", "小时值" if params.granularity == "hour" else "日值"],
                ["数据来源", "算力服时序数据库（主服务器经 MQTT 同步的数据）"],
                ["生成时间", datetime.now(UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")],
                ["说明", "统计值由程序按原始样本计算；无数据的时段留空，不做插值或补齐。"]):
        info.append(row)
    for cell in info["A"]:
        cell.font = bold
    info.column_dimensions["A"].width, info.column_dimensions["B"].width = 14, 90
    data = wb.create_sheet("监测统计")
    data.append(["站点", "站点名称", "设备", "指标", "单位", "时间", "平均值", "最小值", "最大值",
                 "样本数"])
    for row in rows:
        data.append(row)
    qs = wb.create_sheet("数据质量")
    qs.append(["站点", "设备", "指标", "期望样本数", "实际样本数", "完整率", "无数据时段数",
               "时段总数"])
    for q in quality:
        qs.append([q["station"], q["equipment"], METRIC_LABELS[Metric(q["metric"])],
                   q["expected"], q["received"], q["completeness"], q["empty_periods"],
                   q["periods"]])
    for sheet in (data, qs):
        for cell in sheet[1]:
            cell.font = bold
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path.stat().st_size
