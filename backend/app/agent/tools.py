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
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.config import Settings
from app.db.chat_store import Citation
from app.db.work_orders import WorkOrderStore
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeUnavailable
from app.stations.model import METRICS_BY_TYPE, EquipmentType, Metric
from app.stations.service import StationNotFound, StationService
from app.timeseries.iotdb import TimeSeriesError

if TYPE_CHECKING:
    from app.agent.reports import ReportStore
    from app.maintenance.service import MaintenanceService


@dataclass
class TurnContext:
    stations: StationService
    maintenance: "MaintenanceService"
    work_orders: WorkOrderStore
    reports: "ReportStore"
    settings: Settings
    retriever: Retriever | None = None
    user_id: str = "anonymous"
    session_id: str | None = None
    workflow: str | None = None
    kb_version: str | None = None
    sources: list[Citation] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.analysis_timezone)

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
    """列出固定监测站目录（编号、名称、类型、通信方式、采样周期、校准系数、本底值、剂量率一级/二级阈值、
    蓄电池预警/报警电压、探测器及校准有效期）。"""
    ctx = _ctx(config)
    rows = [s.model_dump() | {"dose_configured": s.dose_configured}
            for s in await ctx.stations.stations.all()]
    if not rows:
        return "没有配置任何监测站。"
    return ctx.cite({"kind": "data", "title": "监测站目录", "source": "站点目录快照"},
                    _json(rows))


@tool(parse_docstring=True)
async def get_station_status(station_id: str, config: RunnableConfig) -> str:
    """监测站最新数据：探测器 γ 瞬时值/平均值/标准差（µGy/h，未乘校准系数）、设备状态、通信状态，
    备电电压/电量/市电/充电/参考续航，方舱温湿度、门禁、雨量。

    Args:
        station_id: 监测站编号，例如 AS01。
    """
    ctx = _ctx(config)
    try:
        station = await ctx.stations.station(station_id.upper())
        latest = await ctx.stations.latest(station)
    except StationNotFound:
        return f"监测站 {station_id} 不存在。可先调用 list_stations 查看编号。"
    except TimeSeriesError as exc:
        return f"时序数据库不可用：{exc}"
    return ctx.cite({"kind": "data", "title": f"{station.station_id} {station.name} 最新数据",
                     "source": "IoTDB", "device_id": station.station_id}, _json(latest))


@tool(parse_docstring=True)
async def get_telemetry(station_id: str, config: RunnableConfig, metric: str = "dose_rate",
                        device_id: str | None = None, hours: float = 24,
                        interval_minutes: int = 60) -> str:
    """某指标的分时段聚合曲线（每段平均/最小/最大值与样本数），用于趋势分析。

    Args:
        station_id: 监测站编号。
        metric: dose_rate、dose_rate_avg、dose_rate_std、status、comm（探测器指标，需 device_id，
            省略时取该站第一台探测器）；battery_voltage、battery_level、ac_power、charging、
            runtime_hours、shelter_temp、shelter_humidity、door、rainfall（站级指标）。
        device_id: 探测器编号，例如 AS01-DET-01。
        hours: 回溯小时数，默认 24，最多 744。
        interval_minutes: 聚合间隔分钟数，默认 60；总段数不超过 96。
    """
    ctx = _ctx(config)
    hours = max(0.5, min(hours, 744))
    interval = max(interval_minutes, int(hours * 60 / 96) + 1, 1)
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    try:
        station = await ctx.stations.station(station_id.upper())
        m = Metric(metric)
        if m in METRICS_BY_TYPE[EquipmentType.DETECTOR] and device_id is None and station.devices:
            device_id = station.devices[0].device_id
        data = await ctx.stations.telemetry(station, device_id, m, start, end, interval * 60)
    except StationNotFound:
        return f"监测站或设备不存在：{station_id}/{device_id}。"
    except ValueError as exc:
        return f"参数错误：{exc}"
    except TimeSeriesError as exc:
        return f"时序数据库不可用：{exc}"
    return ctx.cite({"kind": "data", "title": f"{station.station_id} {data['equipment_id']} "
                     f"{metric} 曲线（{interval} 分钟）", "source": "IoTDB",
                     "device_id": data["equipment_id"],
                     "time_range": f"{start.isoformat()}/{end.isoformat()}"}, _json(data))


@tool(parse_docstring=True)
async def run_maintenance_check(station_id: str, config: RunnableConfig, hours: float = 24,
                                analysis_type: str = "general") -> str:
    """用维护规则引擎分析一个站点最近一段时间的探测器和备电（与主服务器定时调用的分析相同的规则）：
    健康等级（watch 需关注 / inspect 建议检修 / insufficient_data 数据不足 / normal 未命中规则）、
    关键依据、建议动作和数据限制。

    Args:
        station_id: 监测站编号。
        hours: 回溯小时数，默认 24，最多 168。
        analysis_type: detector、backup_power 或 general（默认，两者都分析）。
    """
    ctx = _ctx(config)
    end = datetime.now(UTC)
    start = end - timedelta(hours=max(1, min(hours, 168)))
    try:
        res = await ctx.maintenance.assess(station_id.upper(), start, end, analysis_type)
    except Exception as exc:
        return f"分析失败：{getattr(exc, 'message', exc)}"
    return ctx.cite({"kind": "data", "title": f"{station_id.upper()} 维护规则分析",
                     "source": "维护规则引擎", "device_id": station_id.upper(),
                     "time_range": f"{start.isoformat()}/{end.isoformat()}"},
                    res.model_dump_json(exclude={"versions"}))


@tool(parse_docstring=True)
async def get_work_orders(config: RunnableConfig, station_id: str | None = None,
                          days: int = 30) -> str:
    """主服务器推送过来的维护工单副本（风险类型、健康等级、触发时间、已发布 AI 结果、人工审核结论）。

    Args:
        station_id: 监测站编号（可选）。
        days: 回溯天数，默认 30，最多 365。
    """
    ctx = _ctx(config)
    end = datetime.now(UTC)
    items = await ctx.work_orders.list(station_id.upper() if station_id else None, None,
                                       end - timedelta(days=max(1, min(days, 365))), end, 50)
    if not items:
        return "该时间范围内没有工单记录。"
    return ctx.cite({"kind": "data", "title": f"维护工单（近 {days} 天）", "source": "工单副本",
                     "device_id": station_id}, _json([i.model_dump(mode="json",
                                                                   exclude={"received_at"})
                                                      for i in items]))


DATA_TOOLS = [list_stations, get_station_status, get_telemetry, run_maintenance_check,
              get_work_orders]
ALL_TOOLS = [search_knowledge, *DATA_TOOLS]
