"""Assistant conversation handling.

The RAG chain (embedding -> vector recall -> rerank -> vLLM) is not wired yet because the
vector database, embedding/rerank models and model service are not finalised. Until then
the assistant records the question and returns an explicit "knowledge base unavailable"
error instead of generating an ungrounded answer.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from industrial_ai.core.errors import AppError
from industrial_ai.core.security import CallerContext
from industrial_ai.schemas.assistant import ChatMessage, ChatRequest, ChatSession
from industrial_ai.schemas.events import EventType, StreamEvent
from industrial_ai.storage.base import SessionStore


class AssistantService:
    def __init__(
        self, sessions: SessionStore, *, max_message_chars: int, knowledge_index_version: str | None
    ) -> None:
        self._sessions = sessions
        self._max_chars = max_message_chars
        self._index_version = knowledge_index_version

    async def open_session(self, request: ChatRequest, caller: CallerContext) -> ChatSession:
        if len(request.message) > self._max_chars:
            raise AppError("message_too_long", f"问题长度不能超过 {self._max_chars} 个字符",
                           status_code=422)
        if request.session_id is None:
            return await self._sessions.create_session(caller.user_id)
        return await self.get_session(request.session_id, caller)

    async def get_session(self, session_id: str, caller: CallerContext) -> ChatSession:
        session = await self._sessions.get_session(session_id)
        if session is None or session.user_id != caller.user_id:
            raise AppError("session_not_found", "会话不存在", status_code=404)
        return session

    async def list_messages(self, session_id: str, caller: CallerContext) -> list[ChatMessage]:
        await self.get_session(session_id, caller)
        return await self._sessions.list_messages(session_id)

    async def stream_reply(
        self, session: ChatSession, request: ChatRequest
    ) -> AsyncIterator[StreamEvent]:
        seq = 0

        def event(kind: EventType, data: dict) -> StreamEvent:
            nonlocal seq
            seq += 1
            return StreamEvent(seq=seq, event=kind, data=data)

        yield event("stage", {"stage": "understanding", "message": "正在识别问题",
                              "progress": 0.1, "session_id": session.session_id})
        await self._sessions.append_message(ChatMessage(
            message_id=f"msg_{uuid.uuid4().hex}", session_id=session.session_id, role="user",
            content=request.message, created_at=datetime.now(UTC),
            knowledge_index_version=self._index_version,
        ))
        yield event("error", {"code": "knowledge_base_unavailable",
                              "message": "知识库与模型服务尚未接入，无法生成有依据的回答",
                              "retryable": False})
        yield event("done", {"session_id": session.session_id})
