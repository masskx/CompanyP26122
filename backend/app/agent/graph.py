"""The assistant as one LangGraph graph: a router in front of four preset workflows and the
free-conversation agent.

    START -> route --monitoring_report / system_info / dose_assessment / maintenance_advice-->
                 <workflow node: parameters + data collected by code> -> narrate -> END
             route --general--> agent <-> tools (ReAct, knowledge + read-only tools) -> END

Routing: explicit `workflow` from the request > keywords > model classification (with the
recent conversation) > general. Nodes report progress with `stage` events and structured
outputs (e.g. the report download) with `result` events (LangGraph custom stream).
"""

from datetime import UTC, datetime, timedelta

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent import workflows as wf
from app.agent.prompts import NARRATE_PROMPTS, narrate_system, system_prompt


class AssistantState(MessagesState):
    workflow: str
    facts: str


def tool_rounds_this_turn(messages: list) -> int:
    rounds = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, AIMessage) and m.tool_calls:
            rounds += 1
    return rounds


def _stage(stage: str, label: str, **extra) -> None:
    get_stream_writer()({"event": "stage", "stage": stage, "label": label, **extra})


def _question(state: AssistantState) -> str:
    return next((m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
                "")  # type: ignore[return-value]


def _history(state: AssistantState) -> list[str]:
    out = []
    for m in state["messages"][:-1]:
        if isinstance(m, HumanMessage | AIMessage) and isinstance(m.content, str) and m.content:
            out.append(f"{'用户' if isinstance(m, HumanMessage) else '助手'}：{m.content[:300]}")
    return out


def build_graph(llm: BaseChatModel, tools: list[BaseTool], *, max_tool_rounds: int = 4
                ) -> CompiledStateGraph:
    llm_with_tools = llm.bind_tools(tools)
    knowledge = any(t.name == "search_knowledge" for t in tools)

    async def route(state: AssistantState, config: RunnableConfig) -> dict:
        ctx = config["configurable"]["ctx"]
        _stage("intent", "识别意图")
        name = config["configurable"].get("workflow")
        if name not in wf.WORKFLOWS and name != wf.GENERAL:
            question = _question(state)
            name = wf.keyword_route(question)
            if name is None:
                name = await wf.classify(llm, question, _history(state)) \
                    if ctx.settings.assistant_intent_model else wf.GENERAL
        ctx.workflow = name
        if name != wf.GENERAL:
            _stage("intent", f"工作流：{wf.WORKFLOWS[name]['title']}", workflow=name)
        return {"workflow": name}

    async def params(state: AssistantState, config: RunnableConfig) -> dict:
        ctx = config["configurable"]["ctx"]
        stations = await ctx.stations.stations.all()
        return await wf.extract(llm, _question(state), _history(state), stations,
                                datetime.now(UTC), ctx.tz)

    async def system_info(state: AssistantState, config: RunnableConfig) -> dict:
        ctx = config["configurable"]["ctx"]
        _stage("data", "读取全网站点状态")
        p = await params(state, config)
        facts, result = await wf.system_info(ctx, p["site_ids"])
        if result:
            get_stream_writer()({"event": "result", **result})
        return {"facts": facts}

    async def dose_assessment(state: AssistantState, config: RunnableConfig) -> dict:
        ctx = config["configurable"]["ctx"]
        _stage("data", "解析评估站点与时段")
        p = await params(state, config)
        if not p["site_ids"]:
            stations = await ctx.stations.stations.all()
            return {"facts": "缺少参数：评估站点。请用户补充站点编号或名称（例如 AS01 三废区）"
                             "和时间段（例如 10月1日—12日）。可选站点："
                             + "、".join(f"{s.station_id} {s.name}" for s in stations)}
        now = datetime.now(UTC)
        start = p["start"] or now - timedelta(days=7)
        end = min(p["end"] or now, now)
        _stage("data", "统计本期、上月同期与去年同期")
        facts, result = await wf.dose_assessment(ctx, p["site_ids"], start, end)
        if result:
            get_stream_writer()({"event": "result", **result})
        return {"facts": facts}

    async def maintenance_advice(state: AssistantState, config: RunnableConfig) -> dict:
        ctx = config["configurable"]["ctx"]
        _stage("data", "逐站执行维护规则分析")
        p = await params(state, config)
        now = datetime.now(UTC)
        start = p["start"] or now - timedelta(hours=24)
        end = min(p["end"] or now, now)
        facts, result = await wf.maintenance_advice(ctx, p["site_ids"], start, end)
        if result:
            get_stream_writer()({"event": "result", **result})
        return {"facts": facts}

    async def monitoring_report(state: AssistantState, config: RunnableConfig) -> dict:
        ctx = config["configurable"]["ctx"]
        _stage("data", "解析报表参数")
        stations = await ctx.stations.stations.all()
        p = wf.report_defaults(await params(state, config), stations, datetime.now(UTC),
                               ctx.tz, ctx.settings.report_max_days)
        _stage("data", "统计数据并生成 Excel")
        facts, result = await wf.monitoring_report(ctx, p)
        if result:
            get_stream_writer()({"event": "result", **result})
        return {"facts": facts}

    async def narrate(state: AssistantState, config: RunnableConfig) -> dict:
        _stage("generating", "生成回答")
        name = state["workflow"]
        tz = config["configurable"]["ctx"].tz
        messages = [SystemMessage(narrate_system(name, tz)), *state["messages"][:-1],
                    HumanMessage(f"用户问题：{_question(state)}\n\n"
                                 f"以下是程序为「{wf.WORKFLOWS[name]['title']}」取得的数据事实"
                                 f"（带引用编号）：\n{state.get('facts', '')}")]
        return {"messages": [await llm.ainvoke(messages, config)]}

    async def agent(state: AssistantState, config: RunnableConfig) -> dict:
        ctx = config["configurable"]["ctx"]
        can_search = knowledge and ctx.retriever is not None
        rounds = tool_rounds_this_turn(state["messages"])
        _stage("generating" if rounds else "intent", "生成回答" if rounds else "分析问题")
        model = llm if rounds >= max_tool_rounds else llm_with_tools
        response = await model.ainvoke(
            [SystemMessage(system_prompt(can_search)), *state["messages"]], config)
        return {"messages": [response]}

    graph = StateGraph(AssistantState)
    graph.add_node("route", route)
    for name, fn in (("system_info", system_info), ("dose_assessment", dose_assessment),
                     ("maintenance_advice", maintenance_advice),
                     ("monitoring_report", monitoring_report)):
        graph.add_node(name, fn)
        graph.add_edge(name, "narrate")
    graph.add_node("narrate", narrate)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", lambda s: s["workflow"] if s["workflow"] in
                                wf.WORKFLOWS else "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    graph.add_edge("narrate", END)
    assert set(NARRATE_PROMPTS) == set(wf.WORKFLOWS)
    return graph.compile()
