"""Assistant conversations: user-scoped sessions, agent-driven answers, persistence.

Every operation takes the authenticated Principal; sessions are looked up by
(user, session_id), so one user can never read or write another user's conversation.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from industrial_ai.agent.runner import AgentDelta, AgentFinal, AgentRunner, AgentStage
from industrial_ai.agent.tools import ToolContext
from industrial_ai.core.errors import AppError
from industrial_ai.core.identity import Principal
from industrial_ai.integrations.llm_client import LLMError
from industrial_ai.schemas.assistant import (
    ChatMessage,
    ChatRequest,
    ChatSession,
    MessagePage,
    SessionPage,
    UserInfo,
)
from industrial_ai.schemas.events import EventType, StreamEvent
from industrial_ai.storage.conversations import ConversationStore, NewMessage, UserRecord

logger = logging.getLogger(__name__)


def _not_found() -> AppError:
    return AppError("session_not_found", "会话不存在", status_code=404)


@dataclass
class ChatTurn:
    principal: Principal
    user: UserRecord
    session: ChatSession
    question: str
    history: list[ChatMessage]
    user_message: ChatMessage


class AssistantService:
    def __init__(self, store: ConversationStore, runner: AgentRunner | None, *,
                 max_message_chars: int, history_messages: int) -> None:
        self._store = store
        self._runner = runner
        self._max_chars = max_message_chars
        self._history = history_messages

    # ---- users & sessions ---------------------------------------------------------------

    async def me(self, principal: Principal) -> UserInfo:
        user = await self._store.ensure_user(principal)
        return UserInfo(user_id=user.user_id, issuer=user.issuer, subject=user.subject,
                        display_name=user.display_name, roles=list(principal.roles),
                        auth_method=principal.auth_method)

    async def create_session(self, principal: Principal, title: str | None) -> ChatSession:
        user = await self._store.ensure_user(principal)
        return await self._store.create_session(user.user_id, title)

    async def list_sessions(self, principal: Principal, *, limit: int, before: datetime | None,
                            include_archived: bool) -> SessionPage:
        user = await self._store.ensure_user(principal)
        items = await self._store.list_sessions(user.user_id, limit=limit, before=before,
                                                include_archived=include_archived)
        return SessionPage(items=items,
                           next_before=items[-1].updated_at if len(items) == limit else None)

    async def get_session(self, principal: Principal, session_id: str) -> ChatSession:
        user = await self._store.ensure_user(principal)
        session = await self._store.get_session(user.user_id, session_id)
        if session is None:
            raise _not_found()
        return session

    async def update_session(self, principal: Principal, session_id: str, *,
                             title: str | None, archived: bool | None) -> ChatSession:
        user = await self._store.ensure_user(principal)
        session = await self._store.update_session(user.user_id, session_id, title=title,
                                                   archived=archived)
        if session is None:
            raise _not_found()
        return session

    async def delete_session(self, principal: Principal, session_id: str) -> None:
        user = await self._store.ensure_user(principal)
        if not await self._store.delete_session(user.user_id, session_id):
            raise _not_found()

    async def list_messages(self, principal: Principal, session_id: str, *, limit: int,
                            before_seq: int | None) -> MessagePage:
        user = await self._store.ensure_user(principal)
        if await self._store.get_session(user.user_id, session_id) is None:
            raise _not_found()
        items = await self._store.list_messages(user.user_id, session_id, limit=limit,
                                                before_seq=before_seq)
        more = len(items) == limit and items and items[0].seq > 1
        return MessagePage(items=items, next_before_seq=items[0].seq if more else None)

    async def add_feedback(self, principal: Principal, message_id: str, rating: int,
                           comment: str | None) -> None:
        user = await self._store.ensure_user(principal)
        if not await self._store.add_feedback(user.user_id, message_id, rating, comment):
            raise AppError("message_not_found", "消息不存在", status_code=404)

    # ---- chat ---------------------------------------------------------------------------

    async def start_turn(self, principal: Principal, request: ChatRequest) -> ChatTurn:
        """Validate and persist the user's message. Errors here happen before streaming."""
        if self._runner is None:
            raise AppError("llm_unavailable", "大模型服务未配置", status_code=503)
        question = request.message.strip()
        if not question:
            raise AppError("empty_message", "问题不能为空", status_code=422)
        if len(question) > self._max_chars:
            raise AppError("message_too_long", f"问题长度不能超过 {self._max_chars} 个字符",
                           status_code=422)
        user = await self._store.ensure_user(principal)
        if request.session_id is None:
            session = await self._store.create_session(user.user_id, question[:30])
        else:
            found = await self._store.get_session(user.user_id, request.session_id)
            if found is None:
                raise _not_found()
            session = found
        history = await self._store.list_messages(user.user_id, session.session_id,
                                                  limit=self._history, before_seq=None)
        user_message = await self._store.add_message(user.user_id, NewMessage(
            session_id=session.session_id, role="user", content=question))
        return ChatTurn(principal, user, session, question, history, user_message)

    async def stream_turn(self, turn: ChatTurn) -> AsyncIterator[StreamEvent]:
        assert self._runner is not None
        seq = 0

        def event(kind: EventType, data: dict) -> StreamEvent:
            nonlocal seq
            seq += 1
            return StreamEvent(seq=seq, event=kind, data=data)

        streamed: list[str] = []
        final: AgentFinal | None = None
        error: tuple[str, str] | None = None
        saved: ChatMessage | None = None
        ctx = ToolContext(principal=turn.principal)
        try:
            yield event("stage", {"stage": "accepted", "message": "已收到问题",
                                  "session_id": turn.session.session_id,
                                  "user_message_id": turn.user_message.message_id})
            async for item in self._runner.run(turn.history, turn.question, ctx):
                if isinstance(item, AgentStage):
                    yield event("stage", {"stage": item.stage, "message": item.message,
                                          **({"detail": item.detail} if item.detail else {})})
                elif isinstance(item, AgentDelta):
                    streamed.append(item.text)
                    yield event("delta", {"text": item.text})
                else:
                    final = item
            if final is not None and not final.content:
                error = ("empty_answer", "模型未生成回答，请重试")
        except LLMError as exc:
            logger.warning("assistant model failure", extra={"fields": {
                "session_id": turn.session.session_id, "error": str(exc)}})
            error = ("llm_unavailable", "大模型服务暂不可用，请稍后重试")
        except Exception:
            logger.exception("assistant turn failed", extra={"fields": {
                "session_id": turn.session.session_id}})
            error = ("internal_error", "回答生成失败")
        finally:
            # Persist even if the client disconnected (generator cancelled) mid-stream.
            saved = await asyncio.shield(self._save(turn, final, streamed, error))

        if error is not None:
            yield event("error", {"code": error[0], "message": error[1],
                                  "retryable": error[0] != "internal_error"})
        else:
            for c in saved.citations:
                yield event("citation", c.model_dump(exclude_none=True))
            yield event("result", saved.model_dump(mode="json"))
        yield event("done", {"session_id": turn.session.session_id,
                             "message_id": saved.message_id})

    async def _save(self, turn: ChatTurn, final: AgentFinal | None, streamed: list[str],
                    error: tuple[str, str] | None) -> ChatMessage:
        status = "complete" if final and not error else ("error" if error else "interrupted")
        message = NewMessage(
            session_id=turn.session.session_id, role="assistant", status=status,
            content=final.content if final else "".join(streamed),
            error_code=error[0] if error else None,
        )
        if final is not None:
            message.citations = final.citations
            message.knowledge_index_version = final.kb_version
            message.model = final.model
            message.tools_used = final.tools_used
            message.tool_trace = final.tool_trace
            message.prompt_tokens = final.prompt_tokens
            message.completion_tokens = final.completion_tokens
            message.latency_ms = final.latency_ms
        return await self._store.add_message(turn.user.user_id, message)
