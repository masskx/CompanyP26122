"""Tools the assistant model may call. All are read-only.

A tool returns a short preamble plus "sources". The runner numbers sources globally ([1],
[2], …) across all tool calls in one answer, shows those numbers to the model, and turns
the ones the answer cites into Citation records. Add a tool by implementing AgentTool and
registering it in build_tools().
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from industrial_ai.analytics.rules import RuleConfig, assess_metric
from industrial_ai.core.identity import Principal
from industrial_ai.knowledge.retriever import KnowledgeBaseUnavailable, KnowledgeRetriever
from industrial_ai.schemas.assistant import Citation
from industrial_ai.schemas.common import TimeRange
from industrial_ai.services.equipment_service import EquipmentService
from industrial_ai.storage.base import EquipmentCatalog, StorageUnavailableError, TimeSeriesStore


@dataclass
class ToolContext:
    principal: Principal
    kb_version: str | None = None  # pinned on first knowledge search of an answer


@dataclass
class Source:
    citation: Citation  # ref is assigned by the runner
    body: str


@dataclass
class ToolOutput:
    text: str
    sources: list[Source] = field(default_factory=list)
    error: bool = False


class AgentTool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    stage: str
    stage_message: str

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutput: ...


def as_openai_tool(tool: AgentTool) -> dict:
    return {"type": "function", "function": {
        "name": tool.name, "description": tool.description, "parameters": tool.parameters}}


class KnowledgeSearchTool:
    name = "search_knowledge_base"
    description = (
        "检索站内知识库（核电技术文档、技术手册、规程制度、领域资料）。"
        "当问题需要专业知识、设备原理、技术参数、规程要求或文档依据时调用；"
        "寒暄、纯对话性问题或已在上文检索过的内容无需调用。"
        "可多次调用以检索不同方面。")
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description":
                      "检索语句：完整、具体的中文描述，补全对话中的指代（如“它”→具体设备名）"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10,
                      "description": "返回片段数，默认 5"},
        },
        "required": ["query"],
    }
    stage = "retrieving_knowledge"
    stage_message = "正在检索知识库"

    def __init__(self, retriever: KnowledgeRetriever) -> None:
        self._retriever = retriever

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        query = str(args.get("query") or "").strip()[:500]
        if not query:
            return ToolOutput("参数错误：query 不能为空", error=True)
        top_k = args.get("top_k")
        top_k = top_k if isinstance(top_k, int) and 1 <= top_k <= 10 else None
        try:
            result = await self._retriever.search(query, top_k=top_k,
                                                  roles=ctx.principal.roles,
                                                  version=ctx.kb_version)
        except KnowledgeBaseUnavailable:
            return ToolOutput("知识库当前不可用，无法检索依据。请告知用户无法提供有依据的专业回答，"
                              "不要凭记忆作答。", error=True)
        ctx.kb_version = result.kb_version
        if not result.hits:
            return ToolOutput(
                f"知识库（版本 {result.kb_version}）中未检索到与“{query}”相关的资料。")
        sources = [Source(
            citation=Citation(ref=0, kind="knowledge", title=h.title, source=h.source,
                              doc_id=h.doc_id, chunk_id=h.chunk_id, section=h.section,
                              page=h.page, kb_version=result.kb_version,
                              score=round(h.score, 4), snippet=h.text[:200]),
            body=h.text) for h in result.hits]
        return ToolOutput(f"知识库版本 {result.kb_version}，检索到 {len(sources)} 个相关片段：",
                          sources)


class EquipmentHealthTool:
    name = "get_equipment_health"
    description = ("查询某站点设备（如辐射监测点）最近时间窗口内的健康分类、数据完整率和异常原因。"
                   "用于“哪些设备异常/需要关注”等状态类问题。只读。")
    parameters = {
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "description": "站点编号，例如 AS01"},
            "category": {"type": "string", "description": "设备类别（可选），如 isolation_room"},
        },
        "required": ["site_id"],
    }
    stage = "querying_data"
    stage_message = "正在查询设备健康状态"

    def __init__(self, equipment: EquipmentService) -> None:
        self._equipment = equipment

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        site_id = str(args.get("site_id") or "")
        category = args.get("category") or None
        try:
            overview = await self._equipment.health_overview(site_id, category)
        except Exception:
            return ToolOutput("设备数据当前不可用。", error=True)
        if not overview.items:
            return ToolOutput(f"站点 {site_id} 下没有找到设备。")
        body = json.dumps({
            "window": f"{overview.window_start.isoformat()}/{overview.window_end.isoformat()}",
            "counts": overview.counts,
            "items": [{"equipment_id": i.equipment_id, "name": i.name, "category": i.category,
                       "health_level": i.health_level, "data_completeness": i.data_completeness,
                       "reasons": i.reasons[:3]} for i in overview.items],
        }, ensure_ascii=False, default=str)
        window = f"{overview.window_start.isoformat()}/{overview.window_end.isoformat()}"
        return ToolOutput("设备健康概览（规则判定结果）：", [Source(Citation(
            ref=0, kind="data", title=f"{site_id} 设备健康概览", source="IoTDB 时序数据与规则判定",
            time_range=window), body)])


class DeviceReadingsTool:
    name = "get_device_readings"
    description = ("查询单台设备某指标（默认 dose_rate 剂量率，单位 µSv/h）在最近若干小时内的统计："
                   "样本数、最小/最大/均值/最新值、数据完整率、是否超出配置限值。只读。")
    parameters = {
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "equipment_id": {"type": "string"},
            "metric": {"type": "string", "description": "指标名，默认 dose_rate"},
            "hours": {"type": "number", "minimum": 0.1, "maximum": 168,
                      "description": "回溯小时数，默认 24"},
        },
        "required": ["site_id", "equipment_id"],
    }
    stage = "querying_data"
    stage_message = "正在查询设备读数"

    def __init__(self, catalog: EquipmentCatalog, timeseries: TimeSeriesStore,
                 rules: RuleConfig) -> None:
        self._catalog = catalog
        self._timeseries = timeseries
        self._rules = rules

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        site_id, equipment_id = str(args.get("site_id") or ""), str(args.get("equipment_id") or "")
        metric_name = str(args.get("metric") or "dose_rate")
        hours = args.get("hours", 24)
        hours = float(hours) if isinstance(hours, int | float) and 0.1 <= hours <= 168 else 24.0
        equipment = await self._catalog.get(site_id, equipment_id)
        if equipment is None:
            return ToolOutput(f"设备 {site_id}/{equipment_id} 不存在。", error=True)
        metric = next((m for m in equipment.metrics if m.name == metric_name), None)
        if metric is None:
            return ToolOutput(f"设备 {equipment_id} 没有指标 {metric_name}。", error=True)
        now = datetime.now(UTC)
        window = TimeRange(start=now - timedelta(hours=hours), end=now)
        try:
            points = await self._timeseries.query(site_id, equipment_id, metric_name, window)
        except StorageUnavailableError:
            return ToolOutput("时序数据当前不可用。", error=True)
        a = assess_metric(equipment_id, metric, points, window, self._rules)
        body = json.dumps({
            "equipment": equipment.name, "metric": metric_name, "unit": metric.unit,
            "window_hours": hours, "health_level": a.level,
            "samples": a.stats.valid_points, "min": a.stats.minimum, "max": a.stats.maximum,
            "mean": round(a.stats.mean, 6) if a.stats.mean is not None else None,
            "last": a.stats.last_value,
            "last_time": a.stats.last_timestamp.isoformat() if a.stats.last_timestamp else None,
            "completeness": round(a.completeness, 4) if a.completeness is not None else None,
            "thresholds": metric.thresholds.model_dump() if metric.thresholds else None,
            "observations": [e.observation for e in a.evidence],
            "data_gaps": [g.detail for g in a.gaps],
        }, ensure_ascii=False, default=str)
        return ToolOutput("设备读数统计：", [Source(Citation(
            ref=0, kind="data", title=f"{equipment.name} {metric_name} 读数",
            source="IoTDB 时序数据", equipment_id=equipment_id, time_range=window.label()),
            body)])
