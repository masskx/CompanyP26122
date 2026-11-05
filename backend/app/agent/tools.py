"""Agent tools (all read-only). LangChain builds the function-calling schema from each
signature and docstring.

Per-question state lives in `TurnContext`, passed via the run config
(config["configurable"]["ctx"]): service handles, global source numbering ([1], [2], ...
across all tool calls of one answer) and the pinned knowledge version.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.db.chat_store import Citation
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeUnavailable
from app.timeseries.iotdb import TimeSeriesError
from app.timeseries.service import DeviceNotFound, DeviceService, window


@dataclass
class TurnContext:
    devices: DeviceService
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


@tool(parse_docstring=True)
async def list_devices(config: RunnableConfig, site: str | None = None) -> str:
    """列出辐射监测设备（编号、名称、站点、类型、位置、最新剂量率读数）。

    Args:
        site: 站点编号（可选），例如 AS01。
    """
    ctx = _ctx(config)
    devices = ctx.devices.catalog.all(site=site)
    if not devices:
        return "没有找到设备。"
    rows = []
    for d in devices:
        try:
            latest = await ctx.devices.latest(d)
        except TimeSeriesError:
            latest = "时序数据库不可用"
        rows.append({"device_id": d.device_id, "name": d.name, "site": d.site, "type": d.type,
                     "location": d.location, "latest": latest})
    return ctx.cite({"kind": "data", "title": "设备清单与最新读数", "source": "IoTDB"},
                    _json(rows))


@tool(parse_docstring=True)
async def get_site_overview(site: str, config: RunnableConfig, hours: float = 24) -> str:
    """站点所有设备在最近一段时间内的健康分类（规则判定）和发现的问题，
    用于“哪些设备异常/需要关注”类问题。

    Args:
        site: 站点编号，例如 AS01。
        hours: 回溯小时数，默认 24，最多 168。
    """
    ctx = _ctx(config)
    hours = max(0.5, min(hours, 168))
    try:
        overview = await ctx.devices.overview(site, hours)
    except TimeSeriesError as exc:
        return f"时序数据库不可用：{exc}"
    w = overview["window"]
    return ctx.cite({"kind": "data", "title": f"{site} 站设备健康概览", "source": "IoTDB + 规则",
                     "time_range": f"{w['start']}/{w['end']}"}, _json(overview))


@tool(parse_docstring=True)
async def get_device_status(device_id: str, config: RunnableConfig, hours: float = 24) -> str:
    """单台设备在最近一段时间内的剂量率统计（样本数、最小/最大/均值/最新值、数据完整率）、
    配置的限值和规则判定的健康等级。

    Args:
        device_id: 设备编号，例如 AS01-ISO-RM-02。
        hours: 回溯小时数，默认 24，最多 168。
    """
    ctx = _ctx(config)
    try:
        device = ctx.devices.device(device_id)
        start, end = window(max(0.1, min(hours, 168)))
        result = await ctx.devices.assess(device, start, end)
    except DeviceNotFound:
        return f"设备 {device_id} 不存在。可先调用 list_devices 查看设备编号。"
    except TimeSeriesError as exc:
        return f"时序数据库不可用：{exc}"
    return ctx.cite({"kind": "data", "title": f"{device.name} 状态", "source": "IoTDB + 规则",
                     "device_id": device_id,
                     "time_range": f"{start.isoformat()}/{end.isoformat()}"}, _json(result))


@tool(parse_docstring=True)
async def get_telemetry(device_id: str, config: RunnableConfig, hours: float = 6,
                        interval_minutes: int = 10, metric: str = "dose_rate") -> str:
    """设备某指标的分时段聚合曲线（每段平均/最小/最大值与样本数），用于趋势分析。

    Args:
        device_id: 设备编号。
        hours: 回溯小时数，默认 6，最多 168。
        interval_minutes: 聚合间隔分钟数，默认 10；总段数不超过 96。
        metric: 指标名，默认 dose_rate（剂量率，µSv/h）。
    """
    ctx = _ctx(config)
    hours = max(0.5, min(hours, 168))
    interval = max(interval_minutes, int(hours * 60 / 96) + 1, 1)
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    try:
        device = ctx.devices.device(device_id)
        data = await ctx.devices.telemetry(device, metric, start, end, interval * 60)
    except DeviceNotFound:
        return f"设备或指标不存在：{device_id}/{metric}。"
    except TimeSeriesError as exc:
        return f"时序数据库不可用：{exc}"
    return ctx.cite({"kind": "data", "title": f"{device.name} {metric} 曲线（{interval} 分钟）",
                     "source": "IoTDB", "device_id": device_id,
                     "time_range": f"{start.isoformat()}/{end.isoformat()}"}, _json(data))


DATA_TOOLS = [list_devices, get_site_overview, get_device_status, get_telemetry]
ALL_TOOLS = [search_knowledge, *DATA_TOOLS]
