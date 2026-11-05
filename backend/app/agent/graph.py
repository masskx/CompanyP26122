"""The assistant as a LangGraph graph.

    START -> agent --(tool calls?)--> tools -> agent -> ... -> END
                   \\-(no tool calls)-> END

The model decides whether to call tools (knowledge search, device data) or answer directly.
After `max_tool_rounds` rounds of tool calls in one turn the agent node calls the model
without tools, so every turn ends with an answer.
"""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.prompts import system_prompt


def tool_rounds_this_turn(messages: list) -> int:
    rounds = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, AIMessage) and m.tool_calls:
            rounds += 1
    return rounds


def build_graph(llm: BaseChatModel, tools: list[BaseTool], *, max_tool_rounds: int = 4
                ) -> CompiledStateGraph:
    llm_with_tools = llm.bind_tools(tools)
    knowledge = any(t.name == "search_knowledge" for t in tools)

    async def agent(state: MessagesState, config: RunnableConfig) -> dict:
        ctx = config["configurable"]["ctx"]
        can_search = knowledge and ctx.retriever is not None
        model = llm if tool_rounds_this_turn(state["messages"]) >= max_tool_rounds \
            else llm_with_tools
        response = await model.ainvoke(
            [SystemMessage(system_prompt(can_search)), *state["messages"]], config)
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile()
