"""Agent loop: the model decides whether to call tools (knowledge search, read-only data)
or answer directly.

    for step in 1..max_steps:
        stream a completion with tools offered (none on the last step, forcing an answer)
        no tool calls  -> that text is the answer
        tool calls     -> run them, append numbered results, continue

Text is streamed to the caller as it arrives. Sources get global numbers [1], [2], …;
citations returned are the sources the answer refers to by number.
"""

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from industrial_ai.agent.tools import AgentTool, ToolContext, ToolOutput, as_openai_tool
from industrial_ai.integrations.llm_client import LLMClient, LLMError, StreamResult, TextDelta
from industrial_ai.schemas.analysis import ModelInfo
from industrial_ai.schemas.assistant import ChatMessage, Citation

logger = logging.getLogger(__name__)

_REF = re.compile(r"\[(\d{1,3})\]")

SYSTEM_PROMPT = """你是核电站 MES 工作站的智能助手，服务对象是电站运行、维护和管理人员。

工具使用：
- 问题涉及核电技术、设备原理、技术参数、规程制度、手册内容等专业知识时，先调用 search_knowledge_base 检索依据；可按需多次检索不同方面。
- 问题涉及设备当前状态、辐射剂量率读数或异常时，调用 get_equipment_health 或 get_device_readings。
- 寒暄、澄清问题、关于本次对话本身的问题，或上文已检索到依据的追问，可直接回答，不必检索。

回答要求：
- 事实性内容只能依据工具返回的资料或数据，并在相应句子后用 [编号] 标注来源，编号与工具结果中的编号一致。
- 未检索到依据、数据不足或工具不可用时，明确说明“未找到依据”或缺少哪些数据，不得编造事实、数值、文档或引用。
- 不提供设备操作、控制、报警确认或工单流转指令；所有建议仅供授权人员人工核查。
- 用简洁、专业的中文回答，不输出思考过程。
{knowledge_note}
当前时间（UTC）：{now}"""

_NO_KB_NOTE = ("\n注意：知识库当前未接入。涉及专业知识的问题应说明暂时无法检索文档依据，"
               "不要凭记忆作答。")


@dataclass
class AgentStage:
    stage: str
    message: str
    detail: dict[str, Any] | None = None


@dataclass
class AgentDelta:
    text: str


@dataclass
class AgentFinal:
    content: str
    citations: list[Citation]
    kb_version: str | None
    tools_used: list[str]
    tool_trace: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    model: ModelInfo | None = None


AgentEvent = AgentStage | AgentDelta | AgentFinal


class AgentRunner:
    def __init__(self, llm: LLMClient, tools: list[AgentTool], *, model_info: ModelInfo,
                 max_steps: int = 4, tool_result_chars: int = 8000,
                 max_answer_tokens: int = 2048) -> None:
        self._llm = llm
        self._tools = {t.name: t for t in tools}
        self._specs = [as_openai_tool(t) for t in tools]
        self._model_info = model_info
        self._max_steps = max_steps
        self._tool_chars = tool_result_chars
        self._max_tokens = max_answer_tokens

    @property
    def knowledge_enabled(self) -> bool:
        return "search_knowledge_base" in self._tools

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(
            knowledge_note="" if self.knowledge_enabled else _NO_KB_NOTE,
            now=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"))

    async def run(self, history: list[ChatMessage], question: str, ctx: ToolContext
                  ) -> AsyncIterator[AgentEvent]:
        started = time.perf_counter()
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt()}]
        messages += [{"role": m.role, "content": m.content} for m in history
                     if m.status == "complete" and m.content]
        messages.append({"role": "user", "content": question})

        sources: list[Citation] = []
        streamed: list[str] = []
        trace: list[dict] = []
        tools_used: list[str] = []
        prompt_tokens = completion_tokens = 0

        yield AgentStage("understanding", "正在理解问题")
        for step in range(1, self._max_steps + 1):
            offer_tools = self._specs if step < self._max_steps else None
            result: StreamResult | None = None
            async for item in self._llm.stream_chat(messages, tools=offer_tools,
                                                    max_tokens=self._max_tokens):
                if isinstance(item, TextDelta):
                    streamed.append(item.text)
                    yield AgentDelta(item.text)
                else:
                    result = item
            if result is None:
                raise LLMError("empty model stream", retryable=False)
            prompt_tokens += result.prompt_tokens or 0
            completion_tokens += result.completion_tokens or 0
            if not result.tool_calls:
                break

            messages.append({"role": "assistant", "content": result.content, "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in result.tool_calls]})
            for call in result.tool_calls:
                tool = self._tools.get(call.name)
                try:
                    args = json.loads(call.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError
                except ValueError:
                    args = None
                if tool is None or args is None:
                    output = ToolOutput(f"工具 {call.name} 不存在或参数不是合法 JSON。", error=True)
                else:
                    yield AgentStage(tool.stage, tool.stage_message, {
                        "tool": tool.name,
                        **({"query": args["query"]} if "query" in args else {})})
                    output = await tool.run(args, ctx)
                    tools_used.append(tool.name)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": self._render(output, sources)})
                trace.append({"tool": call.name, "arguments": args, "error": output.error,
                              "sources": len(output.sources)})
            yield AgentStage("generating", "正在生成回答")

        content = "".join(streamed).strip()
        yield AgentFinal(
            content=content, citations=self._cited(content, sources), kb_version=ctx.kb_version,
            tools_used=sorted(set(tools_used)), tool_trace=trace,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            latency_ms=round((time.perf_counter() - started) * 1000), model=self._model_info)

    def _render(self, output: ToolOutput, sources: list[Citation]) -> str:
        parts = [output.text]
        for src in output.sources:
            ref = len(sources) + 1
            citation = src.citation.model_copy(update={"ref": ref})
            sources.append(citation)
            header = citation.title + (f" / {citation.section}" if citation.section else "")
            if citation.page:
                header += f"（第 {citation.page} 页）"
            parts.append(f"[{ref}] {header}\n{src.body}")
        text = "\n\n".join(parts)
        return text if len(text) <= self._tool_chars else text[: self._tool_chars] + "\n…（已截断）"

    @staticmethod
    def _cited(content: str, sources: list[Citation]) -> list[Citation]:
        refs = {int(m) for m in _REF.findall(content)}
        return [s for s in sources if s.ref in refs]
