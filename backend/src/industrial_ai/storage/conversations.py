"""Conversation persistence interface and the in-memory implementation.

Every read and write of sessions/messages is scoped by the internal `user_id`, which is
derived from the authenticated Principal (issuer + subject) via `ensure_user`. Callers
cannot address another user's data, even with a valid session id.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from industrial_ai.core.identity import Principal
from industrial_ai.schemas.analysis import ModelInfo
from industrial_ai.schemas.assistant import ChatMessage, ChatSession, Citation


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    issuer: str
    subject: str
    display_name: str | None
    created_at: datetime
    last_seen_at: datetime


@dataclass
class NewMessage:
    session_id: str
    role: str
    content: str
    status: str = "complete"
    citations: list[Citation] = field(default_factory=list)
    knowledge_index_version: str | None = None
    model: ModelInfo | None = None
    tools_used: list[str] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)  # audit only, never returned to clients
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    error_code: str | None = None


class ConversationStore(Protocol):
    async def ensure_user(self, principal: Principal) -> UserRecord: ...

    async def create_session(self, user_id: str, title: str | None) -> ChatSession: ...

    async def get_session(self, user_id: str, session_id: str) -> ChatSession | None: ...

    async def list_sessions(self, user_id: str, *, limit: int, before: datetime | None,
                            include_archived: bool) -> list[ChatSession]: ...

    async def update_session(self, user_id: str, session_id: str, *, title: str | None,
                             archived: bool | None) -> ChatSession | None: ...

    async def delete_session(self, user_id: str, session_id: str) -> bool: ...

    async def add_message(self, user_id: str, message: NewMessage) -> ChatMessage: ...

    async def list_messages(self, user_id: str, session_id: str, *, limit: int,
                            before_seq: int | None) -> list[ChatMessage]:
        """Newest-first page converted to chronological order."""
        ...

    async def add_feedback(self, user_id: str, message_id: str, rating: int,
                           comment: str | None) -> bool: ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._users: dict[tuple[str, str], UserRecord] = {}
        self._sessions: dict[str, tuple[str, ChatSession, bool]] = {}  # id -> (owner, s, deleted)
        self._messages: dict[str, list[ChatMessage]] = {}
        self._message_owner: dict[str, str] = {}
        self.feedback: dict[tuple[str, str], tuple[int, str | None]] = {}
        self.tool_traces: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    async def ensure_user(self, principal: Principal) -> UserRecord:
        now = datetime.now(UTC)
        key = (principal.issuer, principal.subject)
        existing = self._users.get(key)
        record = UserRecord(
            user_id=existing.user_id if existing else new_id("usr"),
            issuer=principal.issuer, subject=principal.subject,
            display_name=principal.display_name or (existing.display_name if existing else None),
            created_at=existing.created_at if existing else now, last_seen_at=now,
        )
        self._users[key] = record
        return record

    def _owned(self, user_id: str, session_id: str) -> ChatSession | None:
        item = self._sessions.get(session_id)
        if item is None or item[0] != user_id or item[2]:
            return None
        return item[1]

    async def create_session(self, user_id: str, title: str | None) -> ChatSession:
        now = datetime.now(UTC)
        session = ChatSession(session_id=new_id("cs"), title=title, message_count=0,
                              created_at=now, updated_at=now)
        self._sessions[session.session_id] = (user_id, session, False)
        self._messages[session.session_id] = []
        return session

    async def get_session(self, user_id: str, session_id: str) -> ChatSession | None:
        return self._owned(user_id, session_id)

    async def list_sessions(self, user_id: str, *, limit: int, before: datetime | None,
                            include_archived: bool) -> list[ChatSession]:
        items = [s for owner, s, deleted in self._sessions.values()
                 if owner == user_id and not deleted and (include_archived or not s.archived)
                 and (before is None or s.updated_at < before)]
        items.sort(key=lambda s: (s.updated_at, s.session_id), reverse=True)
        return items[:limit]

    async def update_session(self, user_id: str, session_id: str, *, title: str | None,
                             archived: bool | None) -> ChatSession | None:
        session = self._owned(user_id, session_id)
        if session is None:
            return None
        changes: dict = {"updated_at": datetime.now(UTC)}
        if title is not None:
            changes["title"] = title
        if archived is not None:
            changes["archived"] = archived
        updated = session.model_copy(update=changes)
        self._sessions[session_id] = (user_id, updated, False)
        return updated

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        session = self._owned(user_id, session_id)
        if session is None:
            return False
        self._sessions[session_id] = (user_id, session, True)
        return True

    async def add_message(self, user_id: str, message: NewMessage) -> ChatMessage:
        async with self._lock:
            session = self._owned(user_id, message.session_id)
            if session is None:
                raise KeyError(message.session_id)
            now = datetime.now(UTC)
            stored = ChatMessage(
                message_id=new_id("msg"), session_id=message.session_id,
                seq=session.message_count + 1, role=message.role,  # type: ignore[arg-type]
                content=message.content, status=message.status,  # type: ignore[arg-type]
                created_at=now, citations=message.citations,
                knowledge_index_version=message.knowledge_index_version, model=message.model,
                tools_used=message.tools_used, error_code=message.error_code,
            )
            self._messages[message.session_id].append(stored)
            self._message_owner[stored.message_id] = user_id
            self.tool_traces[stored.message_id] = message.tool_trace
            self._sessions[message.session_id] = (user_id, session.model_copy(update={
                "message_count": stored.seq, "updated_at": now}), False)
            return stored

    async def list_messages(self, user_id: str, session_id: str, *, limit: int,
                            before_seq: int | None) -> list[ChatMessage]:
        if self._owned(user_id, session_id) is None:
            return []
        items = [m for m in self._messages.get(session_id, [])
                 if before_seq is None or m.seq < before_seq]
        return items[-limit:] if limit else []

    async def add_feedback(self, user_id: str, message_id: str, rating: int,
                           comment: str | None) -> bool:
        if self._message_owner.get(message_id) != user_id:
            return False
        self.feedback[(message_id, user_id)] = (rating, comment)
        return True

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None
