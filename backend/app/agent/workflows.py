"""The assistant's four preset workflows (docs/原型/打开工作站.html, 智能助手首页):

  monitoring_report     生成监测报表      自选历史数据、统计口径与文件内容 -> Excel (3 sheets)
  system_info           MES 系统信息      全网站点、运行状态与监测数据
  dose_assessment       剂量率评估        某站某时段，对比上月与去年同期
  maintenance_advice    全站维修建议      逐站分析设备健康，给出维修建议与优先级
  general               free conversation: knowledge base + read-only tools (ReAct agent)

Each workflow: parameters (model JSON extraction + deterministic validation / defaults) ->
data collected by code (statistics, comparisons and rule results are never produced by the
model) -> facts with citation numbers -> the narrate node writes the answer.
"""

import calendar
import json
import logging
import re
from datetime import UTC, date, datetime, time, timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.reports import REPORT_METRICS, ReportParams, build_report
from app.stations.model import METRIC_LABELS, Metric, Station
from app.timeseries.iotdb import TimeSeriesError

logger = logging.getLogger(__name__)

GENERAL = "general"
WORKFLOWS: dict[str, dict] = {
    "monitoring_report": {
        "title": "生成监测报表", "description": "自选历史数据、统计口径与文件内容",
        "keywords": ["报表", "导出", "excel", "Excel", "表格", "下载数据"]},
    "system_info": {
        "title": "MES 系统信息", "description": "查看全网站点、运行状态与监测数据",
        "keywords": ["系统信息", "全网站点", "站点列表", "运行状态", "在线情况", "哪些站点",
                     "离线站点", "站点概况"]},
    "dose_assessment": {
        "title": "剂量率评估", "description": "某站点某时段剂量率，对比上月与去年同期",
        "keywords": ["剂量率评估", "对比上月", "去年同期", "上月同期", "同比", "环比"]},
    "maintenance_advice": {
        "title": "全站维修建议", "description": "逐站分析设备健康，给出维修建议与优先级",
        "keywords": ["维修建议", "检修建议", "维护建议", "维修优先级", "全站健康", "设备健康"]},
}
SITE_RE = re.compile(r"(?<![A-Za-z0-9])([ABab][Ss]\d{2})(?![0-9])")


def keyword_route(text: str) -> str | None:
    for name, w in WORKFLOWS.items():
        if any(k in text for k in w["keywords"]):
            return name
    return None


async def classify(llm, text: str, history: list[str]) -> str:
    """Model intent classification for messages without a keyword match."""
    options = "\n".join(f"- {k}：{w['title']}（{w['description']}）" for k, w in WORKFLOWS.items())
    prompt = ("判断用户最新问题属于哪个预置工作流，不属于任何工作流时选 general"
              "（知识问答、闲聊、单个设备的简单查询等）。只输出 JSON："
              '{"workflow": "<名称>"}\n可选工作流：\n' + options + "\n- general：其他")
    context = "\n".join(history[-4:])
    try:
        reply = await llm.bind(response_format={"type": "json_object"}).ainvoke(
            [SystemMessage(prompt), HumanMessage(f"最近对话：\n{context}\n\n最新问题：{text}")])
        name = json.loads(str(reply.content)).get("workflow")
    except Exception as exc:  # unreachable model or invalid JSON: free conversation
        logger.info("intent classification failed: %s", exc)
        return GENERAL
    return name if name in WORKFLOWS else GENERAL


# ---- parameters -------------------------------------------------------------------------------

def sites_in(text: str, known: list[Station]) -> list[str]:
    ids = {s.station_id for s in known}
    found = [m.upper() for m in SITE_RE.findall(text)]
    for s in known:  # station names, e.g. "三废区"
        if s.name in text and s.station_id not in found:
            found.append(s.station_id)
    return [x for x in dict.fromkeys(found) if x in ids]


def _range_from_text(text: str, now: datetime, tz) -> tuple[datetime, datetime] | None:
    today = now.astimezone(tz).date()

    def day(d: date, end: bool = False) -> datetime:
        return datetime.combine(d + timedelta(days=1) if end else d, time(0), tz)

    iso = re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if len(iso) >= 2:
        a, b = (date(int(y), int(m), int(d)) for y, m, d in iso[:2])
        return day(a), day(b, end=True)
    m = re.search(r"(\d{1,2})月(\d{1,2})日?\s*[—\-~～至到]+\s*(?:(\d{1,2})月)?(\d{1,2})日?", text)
    if m:
        m1, d1, m2, d2 = int(m[1]), int(m[2]), int(m[3] or m[1]), int(m[4])
        year = today.year if date(today.year, m1, 1) <= today else today.year - 1
        return day(date(year, m1, d1)), day(date(year if m2 >= m1 else year + 1, m2, d2), True)
    m = re.search(r"最近\s*(\d{1,3})\s*(天|日|小时)", text)
    if m:
        delta = timedelta(days=int(m[1])) if m[2] in "天日" else timedelta(hours=int(m[1]))
        return now - delta, now
    if "昨天" in text:
        return day(today - timedelta(days=1)), day(today)
    if "今天" in text:
        return day(today), now
    if "上周" in text or "最近一周" in text:
        return now - timedelta(days=7), now
    return None


async def extract(llm, text: str, history: list[str], stations: list[Station], now: datetime,
                  tz) -> dict:
    """Parameters from the question: model JSON first, regular expressions as fallback.
    Everything is validated afterwards by the workflow."""
    sites = ", ".join(f"{s.station_id} {s.name}" for s in stations)
    metrics = ", ".join(f"{m.value}（{METRIC_LABELS[m]}）" for m in REPORT_METRICS)
    prompt = (f"从用户问题中提取参数。当前时间 {now.astimezone(tz):%Y-%m-%d %H:%M}（{tz.key}）。"
              f"可选站点：{sites}。可选指标：{metrics}。只输出 JSON："
              '{"site_ids": [], "start": "YYYY-MM-DD HH:MM 或 null", "end": "YYYY-MM-DD HH:MM '
              '或 null", "metrics": [], "unit": "µGy/h 或 nGy/h 或 null", "decimals": "整数或 '
              'null", "granularity": "hour 或 day 或 null"}。未提及的参数填 null 或空列表，不要猜测。')
    out: dict = {}
    try:
        reply = await llm.bind(response_format={"type": "json_object"}).ainvoke(
            [SystemMessage(prompt),
             HumanMessage("最近对话：\n" + "\n".join(history[-4:]) + f"\n\n问题：{text}")])
        out = json.loads(str(reply.content))
        if not isinstance(out, dict):
            out = {}
    except Exception as exc:
        logger.info("parameter extraction failed, using regular expressions: %s", exc)
    known = {s.station_id for s in stations}
    out_sites = [str(x).upper() for x in out.get("site_ids") or [] if str(x).upper() in known]
    params: dict = {"site_ids": out_sites or sites_in(text, stations)}
    for key in ("start", "end"):
        try:
            v = out.get(key)
            params[key] = datetime.fromisoformat(v).replace(tzinfo=tz) \
                if v and v != "null" else None
        except (TypeError, ValueError):
            params[key] = None
    if params["start"] is None or params["end"] is None:
        rng = _range_from_text(text, now, tz)
        if rng:
            params["start"], params["end"] = rng
    params["metrics"] = [m for m in (out.get("metrics") or [])
                         if m in {x.value for x in REPORT_METRICS}]
    params["unit"] = out.get("unit") if out.get("unit") in ("µGy/h", "nGy/h") else \
        ("nGy/h" if "nGy" in text else None)
    try:
        params["decimals"] = max(0, min(6, int(out["decimals"]))) \
            if out.get("decimals") not in (None, "null") else None
    except (TypeError, ValueError):
        params["decimals"] = None
    params["granularity"] = out.get("granularity") if out.get("granularity") in \
        ("hour", "day") else ("day" if "日统计" in text or "日值" in text else None)
    return params


def shift_month(d: datetime, months: int) -> datetime:
    m = d.month - months
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def _fmt(v: float | None, nd: int = 4) -> str:
    return "无数据" if v is None else f"{v:.{nd}f}"


# ---- collectors (facts for the narrate node) ----------------------------------------------------

async def system_info(ctx, sites: list[str]) -> tuple[str, dict | None]:
    stations = [s for s in await ctx.stations.stations.all()
                if not sites or s.station_id in sites]
    now = datetime.now(UTC)
    rows, counts = [], {"total": len(stations), "online": 0, "offline": 0, "h1": 0, "h2": 0,
                        "fault": 0, "on_battery": 0}
    for st in stations:
        try:
            latest = await ctx.stations.latest(st)
        except TimeSeriesError as exc:
            return f"时序数据库不可用：{exc}", None
        det = latest["devices"][0] if latest["devices"] else {}
        dose, status = det.get("dose_rate"), (det.get("status") or {}).get("value")
        last = max((v["time"] for v in (dose, det.get("status")) if v), default=None)
        stale = last is None or now - datetime.fromisoformat(last) > \
            timedelta(seconds=st.sampling_interval_s * 3)
        state = "离线" if stale or status == "offline" else \
            {"h1": "H1 一级报警", "h2": "H2 二级报警", "fault": "设备故障"}.get(status, "正常")
        counts["offline" if state == "离线" else "online"] += 1
        for key in ("h1", "h2", "fault"):
            counts[key] += status == key and state != "离线"
        ac = (latest["backup_power"].get("ac_power") or {}).get("value")
        counts["on_battery"] += ac == "off"
        volt = latest["backup_power"].get("battery_voltage")
        rows.append({"站点": st.station_id, "名称": st.name, "类型": st.station_type,
                     "通信方式": {"fiber": "光纤", "4g": "4G"}.get(st.comm_type or "", "-"),
                     "状态": state, "γ瞬时值": dose["value"] if dose else None,
                     "供电": "电池" if ac == "off" else ("市电" if ac == "on" else "-"),
                     "电池电压": volt["value"] if volt else None, "通讯时间": last,
                     "阈值": f"{st.dose_l1}/{st.dose_l2}" if st.dose_configured else "未配置"})
    body = json.dumps({"汇总": counts, "站点": rows}, ensure_ascii=False, default=str)
    return ctx.cite({"kind": "data", "title": "全网站点运行状态", "source": "IoTDB + 站点目录"},
                    body), {"type": "system_info", "counts": counts}


async def dose_assessment(ctx, sites: list[str], start: datetime, end: datetime
                          ) -> tuple[str, dict | None]:
    tz = ctx.tz
    stations = [s for s in await ctx.stations.stations.all() if s.station_id in sites]
    periods = {"本期": (start, end), "上月同期": (shift_month(start, 1), shift_month(end, 1)),
               "去年同期": (shift_month(start, 12), shift_month(end, 12))}
    out, table = [], []
    for st in stations:
        for dev in st.active_devices():
            entry: dict = {"站点": f"{st.station_id} {st.name}", "设备": dev.device_id,
                           "单位": "µGy/h", "校准系数": st.calibration_factor}
            for label, (a, b) in periods.items():
                try:
                    step = max(1, int((b - a).total_seconds()))
                    buckets = await ctx.stations.store.aggregate(st.station_id, dev.device_id,
                                                                 "dose_rate", a, b, step)
                except TimeSeriesError as exc:
                    return f"时序数据库不可用：{exc}", None
                bk = buckets[0] if buckets else None
                expected = int((b - a).total_seconds() // st.sampling_interval_s) or 1
                f = st.calibration_factor
                if bk is None or not bk.count:
                    entry[label] = {"时段": f"{a.astimezone(tz):%Y-%m-%d %H:%M} 至 "
                                            f"{b.astimezone(tz):%Y-%m-%d %H:%M}",
                                    "状态": "无历史数据，不做对比（不按 0 处理）"}
                    continue
                entry[label] = {"时段": f"{a.astimezone(tz):%Y-%m-%d %H:%M} 至 "
                                        f"{b.astimezone(tz):%Y-%m-%d %H:%M}",
                                "均值": round(bk.avg * f, 4), "最小": round(bk.min * f, 4),
                                "最大": round(bk.max * f, 4), "样本数": bk.count,
                                "完整率": round(min(1.0, bk.count / expected), 3)}
            cur = entry["本期"].get("均值")
            for label in ("上月同期", "去年同期"):
                ref = entry[label].get("均值")
                if cur is not None and ref:
                    entry[label]["本期较其变化%"] = round((cur - ref) / ref * 100, 1)
            if st.dose_configured:
                entry["阈值"] = {"一级": st.dose_l1, "二级": st.dose_l2}
            if st.background_dose:
                entry["本底值"] = st.background_dose
            table.append(entry)
    body = json.dumps(table, ensure_ascii=False, default=str)
    out.append(ctx.cite({"kind": "data", "title": "剂量率评估（本期 / 上月同期 / 去年同期）",
                         "source": "IoTDB", "device_id": ",".join(sites),
                         "time_range": f"{start.isoformat()}/{end.isoformat()}"}, body))
    return "\n\n".join(out), {"type": "dose_assessment", "sites": sites,
                              "window": {"start": start, "end": end}}


async def maintenance_advice(ctx, sites: list[str], start: datetime, end: datetime
                             ) -> tuple[str, dict | None]:
    stations = [s for s in await ctx.stations.stations.all()
                if not sites or s.station_id in sites]
    order = {"inspect": 0, "watch": 1, "insufficient_data": 2, "normal": 3}
    results = []
    for st in stations:
        try:
            res = await ctx.maintenance.assess(st.station_id, start, end)
        except Exception as exc:  # one station failing does not stop the overview
            results.append({"站点": f"{st.station_id} {st.name}", "健康等级": "分析失败",
                            "原因": str(exc)[:120], "_order": 4})
            continue
        past = await ctx.work_orders.list(st.station_id, None, end - timedelta(days=30), end, 20)
        results.append({
            "站点": f"{st.station_id} {st.name}", "健康等级": res.health_level,
            "_order": order[res.health_level],
            "关键依据": [f.message for f in res.findings],
            "建议动作": [f"{'主建议' if a.priority == 'primary' else '次建议'}：{a.text}"
                        for a in res.actions],
            "数据限制": [lim.text for lim in res.limitations],
            "近30天工单": len(past)})
    results.sort(key=lambda r: r["_order"])
    for r in results:
        r.pop("_order")
    counts: dict[str, int] = {}
    for r in results:
        counts[r["健康等级"]] = counts.get(r["健康等级"], 0) + 1
    body = json.dumps({"统计": counts, "站点（按优先级排序）": results}, ensure_ascii=False)
    return ctx.cite({"kind": "data", "title": "全站设备健康分析（规则引擎）",
                     "source": "IoTDB + 维护规则 + 工单副本",
                     "time_range": f"{start.isoformat()}/{end.isoformat()}"}, body), \
        {"type": "maintenance_advice", "counts": counts}


async def monitoring_report(ctx, p: dict) -> tuple[str, dict | None]:
    stations = [s for s in await ctx.stations.stations.all() if s.station_id in p["site_ids"]]
    params = ReportParams(site_ids=[s.station_id for s in stations], start=p["start"],
                          end=p["end"], metrics=p["metrics"], unit=p["unit"],
                          decimals=p["decimals"], granularity=p["granularity"])
    try:
        summary = await build_report(params, stations, ctx.stations.store, ctx.settings.reports_dir,
                                     ctx.user_id, ctx.session_id, ctx.tz)
    except TimeSeriesError as exc:
        return f"时序数据库不可用：{exc}", None
    await ctx.reports.add(summary.record)
    rec = summary.record
    url = f"/api/v1/reports/{rec.report_id}/download"
    body = json.dumps({"报表": rec.title, "文件": rec.file_name, "统计行数": rec.row_count,
                       "工作表": ["报表说明", "监测统计", "数据质量"],
                       "统计摘要": summary.stats, "数据质量": summary.quality},
                      ensure_ascii=False, default=str)
    return ctx.cite({"kind": "data", "title": rec.title, "source": "IoTDB（统计由程序生成）",
                     "time_range": f"{params.start.isoformat()}/{params.end.isoformat()}"},
                    body), {"type": "report", "report_id": rec.report_id, "title": rec.title,
                            "file_name": rec.file_name, "download_url": url,
                            "row_count": rec.row_count, "size_bytes": rec.size_bytes}


def report_defaults(p: dict, stations: list[Station], now: datetime, tz, max_days: int) -> dict:
    today = now.astimezone(tz).date()
    p = dict(p)
    p["site_ids"] = p["site_ids"] or [s.station_id for s in stations]
    if not p.get("start") or not p.get("end"):
        p["start"] = datetime.combine(today - timedelta(days=1), time(0), tz)
        p["end"] = datetime.combine(today, time(0), tz)
    p["end"] = min(p["end"], now)
    if p["end"] - p["start"] > timedelta(days=max_days):
        p["start"] = p["end"] - timedelta(days=max_days)
    p["metrics"] = [Metric(m) for m in p.get("metrics") or [Metric.DOSE_RATE]]
    p["unit"] = p.get("unit") or "µGy/h"
    p["decimals"] = 3 if p.get("decimals") is None else p["decimals"]
    p["granularity"] = p.get("granularity") or \
        ("day" if p["end"] - p["start"] > timedelta(days=3) else "hour")
    return p

