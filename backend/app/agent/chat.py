"""Chat turns: load history, run the graph with streaming, persist the result.

Events yielded by `stream()` (sent to clients as SSE; every payload carries a monotonically
increasing `seq`):
    session   {session_id, user_message_id}
    stage     {stage, label, workflow?}     progress: intent / data / knowledge / generating
    tool      {name, args}                  the general agent called a tool
    result    {type, ...}                   structured result of a workflow (e.g. report download)
    delta     {text}                        answer text as it is generated
    citation  {ref, kind, title, ...}       a source the answer cites
    done      {message: <stored assistant message>, workflow}
    error     {code, message}
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from app.agent.reports import ReportStore
from app.agent.tools import TurnContext
from app.config import Settings
from app.db.chat_store import ChatStore, Message, NewMessage, Session
from app.db.work_orders import WorkOrderStore
from app.maintenance.service import MaintenanceService
from app.rag.retriever import Retriever
from app.stations.service import StationService

logger = logging.getLogger(__name__)
_REF = re.compile(r"\[(\d{1,3})\]")
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class ChatError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class ChatService:
    def __init__(self, store: ChatStore, graph: CompiledStateGraph | None,
                 stations: StationService, maintenance: MaintenanceService,
                 work_orders: WorkOrderStore, reports: ReportStore, settings: Settings,
                 retriever: Retriever | None, *, model: str | None,
                 history_messages: int = 20, max_input_chars: int = 4000,
                 max_tool_rounds: int = 4) -> None:
        self.store = store
        self.graph = graph
        self.stations = stations
        self.maintenance = maintenance
        self.work_orders = work_orders
        self.reports = reports
        self.settings = settings
        self.retriever = retriever
        self.model = model
        self._history = history_messages
        self._max_chars = max_input_chars
        self._recursion = 2 * max_tool_rounds + 5

    async def start(self, user_id: str, question: str, session_id: str | None
                    ) -> tuple[Session, list[Message], Message]:
        """Validate, create/find the session, store the question. Raises ChatError."""
        if self.graph is None:
            raise ChatError("llm_not_configured", "LLM_PROVIDER is none", 503)
        question = question.strip()
        if not question or len(question) > self._max_chars:
            raise ChatError("invalid_message", f"message must be 1-{self._max_chars} characters")
        if session_id:
            session = await self.store.get_session(user_id, session_id)
            if session is None:
                raise ChatError("session_not_found", "session not found", 404)
        else:
            session = await self.store.create_session(user_id, question[:40])
        history = await self.store.messages(user_id, session.session_id, self._history)
        user_msg = await self.store.add_message(user_id, session.session_id,
                                                NewMessage(role="user", content=question))
        return session, history, user_msg

    async def stream(self, user_id: str, session: Session, history: list[Message],
                     user_msg: Message, workflow: str | None = None
                     ) -> AsyncIterator[tuple[str, dict]]:
        assert self.graph is not None
        ctx = TurnContext(stations=self.stations, maintenance=self.maintenance,
                          work_orders=self.work_orders, reports=self.reports,
                          settings=self.settings, retriever=self.retriever, user_id=user_id,
                          session_id=session.session_id)
        inputs = {"messages": [*[HumanMessage(m.content) if m.role == "user"
                                 else AIMessage(m.content)
                                 for m in history if m.status == "complete" and m.content],
                               HumanMessage(user_msg.content)]}
        config = {"configurable": {"ctx": ctx, "workflow": workflow},
                  "recursion_limit": self._recursion}
        streamed, final, tool_calls, error = [], None, [], None
        usage = {"input_tokens": 0, "output_tokens": 0}
        t0 = time.perf_counter()
        seq = 0

        def event(name: str, data: dict) -> tuple[str, dict]:
            nonlocal seq
            seq += 1
            return name, {**data, "seq": seq}

        yield event("session", {"session_id": session.session_id,
                                "user_message_id": user_msg.message_id})
        try:
            async for mode, chunk in self.graph.astream(
                    inputs, config, stream_mode=["messages", "updates", "custom"]):
                if mode == "custom":
                    name = chunk.pop("event", "stage")
                    yield event(name, chunk)
                elif mode == "messages":
                    msg, meta = chunk
                    if meta.get("langgraph_node") in ("agent", "narrate") and \
                            isinstance(msg, AIMessageChunk) and isinstance(msg.content, str) \
                            and msg.content:
                        streamed.append(msg.content)
                        yield event("delta", {"text": msg.content})
                else:
                    for node in ("agent", "narrate"):
                        if node not in chunk:
                            continue
                        ai: AIMessage = chunk[node]["messages"][-1]
                        for k in usage:
                            usage[k] += (ai.usage_metadata or {}).get(k, 0)
                        for call in ai.tool_calls:
                            tool_calls.append({"name": call["name"], "args": call["args"]})
                            yield event("tool", {"name": call["name"], "args": call["args"]})
                        if not ai.tool_calls:
                            final = ai
        except Exception as exc:
            logger.exception("assistant turn failed")
            error = f"{type(exc).__name__}: {exc}"[:250]

        if ctx.workflow and ctx.workflow != "general":
            with contextlib.suppress(Exception):
                await self.store.set_workflow(user_id, session.session_id, ctx.workflow)
        text = _THINK.sub("", final.content if final and isinstance(final.content, str)
                          else "".join(streamed)).strip()
        refs = {int(r) for r in _REF.findall(text)}
        citations = [c for c in ctx.sources if c.ref in refs]
        status = "error" if error or not text else "complete"
        stored = await asyncio.shield(self.store.add_message(user_id, session.session_id,
                                                             NewMessage(
            role="assistant", content=text, status=status, citations=citations,
            tool_calls=tool_calls, model=self.model, kb_version=ctx.kb_version,
            prompt_tokens=usage["input_tokens"], completion_tokens=usage["output_tokens"],
            latency_ms=round((time.perf_counter() - t0) * 1000),
            error=error or (None if text else "empty answer"))))
        if status == "error":
            yield event("error", {"code": "agent_failed", "message": error or "empty answer",
                                  "message_id": stored.message_id})
            return
        for c in citations:
            yield event("citation", c.model_dump(exclude_none=True))
        yield event("done", {"message": stored.model_dump(mode="json"),
                             "workflow": ctx.workflow})

    async def ask(self, user_id: str, question: str, session_id: str | None,
                  workflow: str | None = None) -> dict:
        """Non-streaming variant: returns the stored assistant message (+ workflow results)."""
        session, history, user_msg = await self.start(user_id, question, session_id)
        result: dict = {"session_id": session.session_id, "results": []}
        async for event, data in self.stream(user_id, session, history, user_msg, workflow):
            if event == "done":
                result["message"] = data["message"]
                result["workflow"] = data["workflow"]
            elif event == "result":
                result["results"].append(data)
            elif event == "error":
                raise ChatError(data["code"], data["message"], 502)
        return result
