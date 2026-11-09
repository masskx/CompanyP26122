"""Chat turns: load history, run the graph with streaming, persist the result.

Events yielded by `stream()` (sent to clients as SSE):
    session   {session_id, user_message_id}
    tool      {name, args}                 the agent called a tool
    delta     {text}                       answer text as it is generated
    citation  {ref, kind, title, ...}      a source the answer cites
    done      {message: <stored assistant message>}
    error     {code, message}
"""

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from app.agent.tools import TurnContext
from app.analysis.batch import AnalysisService
from app.db.chat_store import ChatStore, Message, NewMessage, Session
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
                 stations: StationService, analysis: AnalysisService,
                 retriever: Retriever | None, *, model: str | None,
                 history_messages: int = 20, max_input_chars: int = 4000,
                 max_tool_rounds: int = 4) -> None:
        self.store = store
        self.graph = graph
        self.stations = stations
        self.analysis = analysis
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
                     user_msg: Message) -> AsyncIterator[tuple[str, dict]]:
        assert self.graph is not None
        ctx = TurnContext(stations=self.stations, analysis=self.analysis,
                          retriever=self.retriever, user_id=user_id)
        inputs = {"messages": [*[HumanMessage(m.content) if m.role == "user"
                                 else AIMessage(m.content)
                                 for m in history if m.status == "complete" and m.content],
                               HumanMessage(user_msg.content)]}
        config = {"configurable": {"ctx": ctx}, "recursion_limit": self._recursion}
        streamed, final, tool_calls, error = [], None, [], None
        usage = {"input_tokens": 0, "output_tokens": 0}
        t0 = time.perf_counter()
        yield "session", {"session_id": session.session_id, "user_message_id": user_msg.message_id}
        try:
            async for mode, chunk in self.graph.astream(inputs, config,
                                                        stream_mode=["messages", "updates"]):
                if mode == "messages":
                    msg, meta = chunk
                    if meta.get("langgraph_node") == "agent" and isinstance(msg, AIMessageChunk) \
                            and isinstance(msg.content, str) and msg.content:
                        streamed.append(msg.content)
                        yield "delta", {"text": msg.content}
                elif "agent" in chunk:
                    ai: AIMessage = chunk["agent"]["messages"][-1]
                    for k in usage:
                        usage[k] += (ai.usage_metadata or {}).get(k, 0)
                    for call in ai.tool_calls:
                        tool_calls.append({"name": call["name"], "args": call["args"]})
                        yield "tool", {"name": call["name"], "args": call["args"]}
                    if not ai.tool_calls:
                        final = ai
        except Exception as exc:
            logger.exception("agent turn failed")
            error = f"{type(exc).__name__}: {exc}"[:250]

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
            yield "error", {"code": "agent_failed", "message": error or "empty answer",
                            "message_id": stored.message_id}
            return
        for c in citations:
            yield "citation", c.model_dump(exclude_none=True)
        yield "done", {"message": stored.model_dump(mode="json")}

    async def ask(self, user_id: str, question: str, session_id: str | None) -> dict:
        """Non-streaming variant: returns the stored assistant message."""
        session, history, user_msg = await self.start(user_id, question, session_id)
        result: dict = {"session_id": session.session_id}
        async for event, data in self.stream(user_id, session, history, user_msg):
            if event == "done":
                result["message"] = data["message"]
            elif event == "error":
                raise ChatError(data["code"], data["message"], 502)
        return result
