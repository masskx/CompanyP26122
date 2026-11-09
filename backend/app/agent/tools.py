"""Agent tools (all read-only). LangChain builds the function-calling schema from each
signature and docstring.

Per-question state lives in `TurnContext`, passed via the run config
(config["configurable"]["ctx"]): service handles, global source numbering ([1], [2], ...
across all tool calls of one answer) and the pinned knowledge version.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.db.chat_store import Citation
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeUnavailable
from app.stations.model import Metric
from app.stations.service import StationNotFound, StationService
from app.timeseries.iotdb import TimeSeriesError

if TYPE_CHECKING:
    from app.analysis.batch import AnalysisService


@dataclass
class TurnContext:
    stations: StationService
    analysis: "AnalysisService"
    retriever: Retriever | None = None
    user_id: str = "anonymous"
    kb_version: str | None = None
    sources: list[Citation] = field(default_factory=list)

    def cite(self, citation: dict, body: str) -> str:
        c = Citation(ref=len(self.sources) + 1, **citation)
        self.sources.append(c)
        header = c.title + (f" / {c.section}" if c.section else "")
        return f"[{c.ref}] {header}\n{body}"


def _ctx(config: RunnableConfig) -> TurnContext:
    return config["configurable"]["ctx"]


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


@tool(parse_docstring=True)
async def search_knowledge(query: str, config: RunnableConfig, top_k: int = 5) -> str:
    """检索站内知识库（核电技术文档、技术手册、规程制度、领域资料）。
    回答涉及任何技术事实（数值、单位换算、参数、限值、原理、规程、文档内容）或用户要求依据时
    必须调用，即使你认为自己知道答案。可多次调用以检索不同方面。

    Args:
        query: 完整、具体的中文检索语句，补全对话中的指代（如“它”→具体设备名）。
        top_k: 返回片段数，1-10，默认 5。
    """
    ctx = _ctx(config)
    if ctx.retriever is None:
        return "知识库未配置，无法检索依据。请告知用户无法提供有依据的专业回答。"
    try:
        version, hits = await ctx.retriever.search(query, max(1, min(top_k, 10)), ctx.kb_version)
    except KnowledgeUnavailable as exc:
        return f"知识库当前不可用（{exc}）。请告知用户无法提供有依据的专业回答，不要凭记忆作答。"
    ctx.kb_version = version  # later searches in this answer use the same version
    if not hits:
        return f"知识库（版本 {version}）中未检索到与“{query}”相关的资料。"
    parts = [ctx.cite({"kind": "knowledge", "title": h.title, "source": h.source or None,
                       "doc_id": h.doc_id, "section": h.section or None, "page": h.page,
                       "kb_version": version, "score": round(h.score, 4),
                       "snippet": h.text[:200]}, h.text) for h in hits]
    return f"知识库版本 {version}，检索到 {len(hits)} 个片段：\n\n" + "\n\n".join(parts)


@tool
async def list_stations(config: RunnableConfig) -> str:
    """列出固定监测站及其配置（编号、名称、采样周期、校准系数、剂量率一级/二级阈值、
    蓄电池预警/报警电压、设备清单）。"""
    ctx = _ctx(config)
    rows = [s.model_dump() | {"dose_configured": s.dose_configured,
                              "battery_configured": s.battery_configured}
            for s in await ctx.stations.stations.all()]
    if not rows:
        return "没有配置任何监测站。"
    return ctx.cite({"kind": "data", "title": "监测站配置", "source": "openGauss station_config"},
                    _json(rows))


@tool(parse_docstring=True)
async def get_station_status(station_id: str, config: RunnableConfig) -> str:
    """监测站的最新数据：每台设备的剂量率（µGy/h，未乘校准系数）、设备状态、通信状态，
    以及站级蓄电池电压/电量、市电、充电、方舱温湿度、门禁、雨量。

    Args:
        station_id: 监测站编号，例如 ST01。
    """
    ctx = _ctx(config)
    try:
        station = await ctx.stations.station(station_id)
        latest = await ctx.stations.latest(station)
    except StationNotFound:
        return f"监测站 {station_id} 不存在。可先调用 list_stations 查看编号。"
    except TimeSeriesError as exc:
        return f"时序数据库不可用：{exc}"
    return ctx.cite({"kind": "data", "title": f"{station.name} 最新数据", "source": "IoTDB",
                     "device_id": station_id}, _json(latest))


@tool(parse_docstring=True)
async def get_telemetry(station_id: str, config: RunnableConfig, device_id: str | None = None,
                        metric: str = "dose_rate", hours: float = 6,
                        interval_minutes: int = 10) -> str:
    """某指标的分时段聚合曲线（每段平均/最小/最大值与样本数），用于趋势分析。

    Args:
        station_id: 监测站编号。
        device_id: 设备编号（设备指标 dose_rate/status/comm 必填；站级指标留空）。
        metric: dose_rate（µGy/h）、status、comm、battery_voltage、battery_level、ac_power、
            charging、shelter_temp、shelter_humidity、door、rainfall。
        hours: 回溯小时数，默认 6，最多 168。
        interval_minutes: 聚合间隔分钟数，默认 10；总段数不超过 96。
    """
    ctx = _ctx(config)
    hours = max(0.5, min(hours, 168))
    interval = max(interval_minutes, int(hours * 60 / 96) + 1, 1)
    start, end = datetime.now(UTC) - timedelta(hours=hours), datetime.now(UTC)
    try:
        station = await ctx.stations.station(station_id)
        data = await ctx.stations.telemetry(station, device_id, Metric(metric), start, end,
                                            interval * 60)
    except StationNotFound:
        return f"监测站或设备不存在：{station_id}/{device_id}。"
    except ValueError as exc:
        return f"参数错误：{exc}"
    except TimeSeriesError as exc:
        return f"时序数据库不可用：{exc}"
    return ctx.cite({"kind": "data", "title": f"{station.name} {device_id or '站级'} {metric} "
                     f"曲线（{interval} 分钟）", "source": "IoTDB",
                     "device_id": f"{station_id}/{device_id or '-'}",
                     "time_range": f"{start.isoformat()}/{end.isoformat()}"}, _json(data))


@tool(parse_docstring=True)
async def get_analysis_results(config: RunnableConfig, station_id: str | None = None,
                               date: str | None = None) -> str:
    """每日分析结果（规则判定的异常记录：每台异常设备 / 站级蓄电池一条，含等级、事件数、
    标题和分析摘要）。默认最近一次分析（通常为昨天）。用于“哪些设备有问题/需要检修”类问题。

    Args:
        station_id: 监测站编号（可选）。
        date: 分析日期 YYYY-MM-DD（可选）。
    """
    ctx = _ctx(config)
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date() if date else None
    except ValueError:
        return "date 格式应为 YYYY-MM-DD。"
    store = ctx.analysis.store
    batch = await store.current_batch(ctx.analysis.version, day)
    if batch is None:
        return "没有可用的分析结果" + (f"（{date}）" if date else "") + "。"
    records, total = await store.list_records(batch.batch_id, {"station_id": station_id}, 50, 0)
    rows = [{"record_id": r.record_id, "station": r.station_id, "device": r.device_id,
             "device_name": r.device_name, "level": r.level.value, "title": r.title,
             "event_count": r.event_count, "data_status": r.data_status,
             "summary": r.explanation.summary if r.explanation else None} for r in records]
    body = {"analysis_date": batch.analysis_date.isoformat(), "batch_status": batch.status,
            "config_gaps": batch.config_gaps, "records_total": total, "records": rows}
    return ctx.cite({"kind": "data", "title": f"{batch.analysis_date} 分析结果",
                     "source": "openGauss analysis_record",
                     "time_range": f"{batch.window_start.isoformat()}/"
                                   f"{batch.window_end.isoformat()}"}, _json(body))


@tool(parse_docstring=True)
async def get_analysis_record(record_id: str, config: RunnableConfig) -> str:
    """一条分析记录的详情：各异常事件的起止时间、统计值、触发规则与阈值、同比/环比、
    上下文（市电、降雨、方舱环境）和分析说明（不含原始曲线）。

    Args:
        record_id: 记录编号（来自 get_analysis_results）。
    """
    ctx = _ctx(config)
    record = await ctx.analysis.store.get_record(record_id)
    if record is None:
        return f"记录 {record_id} 不存在。"
    for e in record.events or []:
        e.values = None
    return ctx.cite({"kind": "data", "title": f"{record.station_id}/{record.device_id} "
                     f"{record.analysis_date} 分析记录", "source": "openGauss analysis_event",
                     "device_id": f"{record.station_id}/{record.device_id}"},
                    record.model_dump_json())


DATA_TOOLS = [list_stations, get_station_status, get_telemetry, get_analysis_results,
              get_analysis_record]
ALL_TOOLS = [search_knowledge, *DATA_TOOLS]
