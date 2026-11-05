"""Conversation storage: sessions and messages, always scoped by user_id.

`PgChatStore` uses openGauss (psycopg 3, plain SQL, PostgreSQL 9.2 dialect).
`MemoryChatStore` has the same interface for tests and database-free runs.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Citation(BaseModel):
    ref: int
    kind: Literal["knowledge", "data"]
    title: str
    source: str | None = None
    doc_id: str | None = None
    section: str | None = None
    page: int | None = None
    kb_version: str | None = None
    score: float | None = None
    device_id: str | None = None
    time_range: str | None = None
    snippet: str | None = None


class Session(BaseModel):
    session_id: str
    user_id: str
    title: str | None
    message_count: int
    created_at: datetime
    updated_at: datetime


class Message(BaseModel):
    message_id: str
    session_id: str
    seq: int
    role: Literal["user", "assistant"]
    content: str
    status: Literal["complete", "error", "interrupted"] = "complete"
    citations: list[Citation] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    model: str | None = None
    kb_version: str | None = None
    error: str | None = None
    feedback: int | None = None
    created_at: datetime


class NewMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    status: Literal["complete", "error", "interrupted"] = "complete"
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[dict] = Field(default_factory=list)
    model: str | None = None
    kb_version: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None


class ChatStore(Protocol):
    async def ping(self) -> None: ...
    async def close(self) -> None: ...
    async def create_session(self, user_id: str, title: str | None) -> Session: ...
    async def get_session(self, user_id: str, session_id: str) -> Session | None: ...
    async def list_sessions(self, user_id: str, limit: int, offset: int) -> list[Session]: ...
    async def rename_session(self, user_id: str, session_id: str, title: str) -> Session | None:
        ...
    async def delete_session(self, user_id: str, session_id: str) -> bool: ...
    async def add_message(self, user_id: str, session_id: str, msg: NewMessage) -> Message: ...
    async def messages(self, user_id: str, session_id: str, limit: int,
                       before_seq: int | None = None) -> list[Message]: ...
    async def set_feedback(self, user_id: str, message_id: str, rating: int,
                           comment: str | None) -> bool: ...


# ---- in-memory ---------------------------------------------------------------------------

class MemoryChatStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._deleted: set[str] = set()
        self._messages: dict[str, list[Message]] = {}
        self.tool_calls: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def _owned(self, user_id: str, session_id: str) -> Session | None:
        s = self._sessions.get(session_id)
        return s if s and s.user_id == user_id and session_id not in self._deleted else None

    async def create_session(self, user_id: str, title: str | None) -> Session:
        now = datetime.now(UTC)
        s = Session(session_id=new_id("s"), user_id=user_id, title=title, message_count=0,
                    created_at=now, updated_at=now)
        self._sessions[s.session_id] = s
        self._messages[s.session_id] = []
        return s

    async def get_session(self, user_id: str, session_id: str) -> Session | None:
        return self._owned(user_id, session_id)

    async def list_sessions(self, user_id: str, limit: int, offset: int) -> list[Session]:
        items = sorted((s for s in self._sessions.values()
                        if s.user_id == user_id and s.session_id not in self._deleted),
                       key=lambda s: s.updated_at, reverse=True)
        return items[offset:offset + limit]

    async def rename_session(self, user_id: str, session_id: str, title: str) -> Session | None:
        s = self._owned(user_id, session_id)
        if s is None:
            return None
        s = s.model_copy(update={"title": title, "updated_at": datetime.now(UTC)})
        self._sessions[session_id] = s
        return s

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        if self._owned(user_id, session_id) is None:
            return False
        self._deleted.add(session_id)
        return True

    async def add_message(self, user_id: str, session_id: str, msg: NewMessage) -> Message:
        async with self._lock:
            s = self._owned(user_id, session_id)
            if s is None:
                raise KeyError(session_id)
            now = datetime.now(UTC)
            m = Message(message_id=new_id("m"), session_id=session_id, seq=s.message_count + 1,
                        created_at=now, tools_used=sorted({c["name"] for c in msg.tool_calls}),
                        **msg.model_dump(include={"role", "content", "status", "citations",
                                                  "model", "kb_version", "error"}))
            self._messages[session_id].append(m)
            self.tool_calls[m.message_id] = msg.tool_calls
            self._sessions[session_id] = s.model_copy(update={"message_count": m.seq,
                                                              "updated_at": now})
            return m

    async def messages(self, user_id: str, session_id: str, limit: int,
                       before_seq: int | None = None) -> list[Message]:
        if self._owned(user_id, session_id) is None:
            return []
        items = [m for m in self._messages[session_id] if before_seq is None or m.seq < before_seq]
        return items[-limit:] if limit else []

    async def set_feedback(self, user_id: str, message_id: str, rating: int,
                           comment: str | None) -> bool:
        for sid, items in self._messages.items():
            for i, m in enumerate(items):
                if m.message_id == message_id and self._owned(user_id, sid):
                    items[i] = m.model_copy(update={"feedback": rating})
                    return True
        return False


# ---- openGauss ---------------------------------------------------------------------------

_SESSION = "session_id, user_id, title, message_count, created_at, updated_at"
_MESSAGE = ("m.message_id, m.session_id, m.seq, m.role, m.content, m.status, m.citations, "
            "m.tool_calls, m.model, m.kb_version, m.error, m.feedback, m.created_at")


def _message(row: dict) -> Message:
    citations = json.loads(row.pop("citations") or "[]")
    tool_calls = json.loads(row.pop("tool_calls") or "[]")
    return Message(**row, citations=citations,
                   tools_used=sorted({c.get("name", "") for c in tool_calls} - {""}))


class PgChatStore:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False,
                                         kwargs={"row_factory": dict_row})
        self._opened = False

    async def _conn(self) -> Any:
        if not self._opened:
            await self._pool.open(wait=False)
            self._opened = True
        return self._pool.connection()

    async def ping(self) -> None:
        async with await self._conn() as conn:
            await conn.execute("SELECT 1 FROM chat_session WHERE 1 = 0")

    async def close(self) -> None:
        if self._opened:
            await self._pool.close()
            self._opened = False

    async def create_session(self, user_id: str, title: str | None) -> Session:
        now = datetime.now(UTC)
        s = Session(session_id=new_id("s"), user_id=user_id, title=title, message_count=0,
                    created_at=now, updated_at=now)
        async with await self._conn() as conn:
            await conn.execute(
                "INSERT INTO chat_session (session_id, user_id, title, message_count, created_at,"
                " updated_at) VALUES (%s, %s, %s, 0, %s, %s)",
                (s.session_id, user_id, title, now, now))
        return s

    async def get_session(self, user_id: str, session_id: str) -> Session | None:
        async with await self._conn() as conn:
            row = await (await conn.execute(
                f"SELECT {_SESSION} FROM chat_session WHERE session_id = %s AND user_id = %s "
                "AND deleted_at IS NULL", (session_id, user_id))).fetchone()
        return Session(**row) if row else None

    async def list_sessions(self, user_id: str, limit: int, offset: int) -> list[Session]:
        async with await self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT {_SESSION} FROM chat_session WHERE user_id = %s AND deleted_at IS NULL "
                "ORDER BY updated_at DESC LIMIT %s OFFSET %s", (user_id, limit, offset))).fetchall()
        return [Session(**r) for r in rows]

    async def rename_session(self, user_id: str, session_id: str, title: str) -> Session | None:
        async with await self._conn() as conn:
            row = await (await conn.execute(
                "UPDATE chat_session SET title = %s, updated_at = %s WHERE session_id = %s "
                f"AND user_id = %s AND deleted_at IS NULL RETURNING {_SESSION}",
                (title, datetime.now(UTC), session_id, user_id))).fetchone()
        return Session(**row) if row else None

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        async with await self._conn() as conn:
            cur = await conn.execute(
                "UPDATE chat_session SET deleted_at = %s WHERE session_id = %s AND user_id = %s "
                "AND deleted_at IS NULL", (datetime.now(UTC), session_id, user_id))
            return cur.rowcount == 1

    async def add_message(self, user_id: str, session_id: str, msg: NewMessage) -> Message:
        now = datetime.now(UTC)
        message_id = new_id("m")
        async with await self._conn() as conn, conn.transaction():
            # Allocate the next seq atomically (row lock) and check ownership.
            row = await (await conn.execute(
                "UPDATE chat_session SET message_count = message_count + 1, updated_at = %s "
                "WHERE session_id = %s AND user_id = %s AND deleted_at IS NULL "
                "RETURNING message_count", (now, session_id, user_id))).fetchone()
            if row is None:
                raise KeyError(session_id)
            await conn.execute(
                "INSERT INTO chat_message (message_id, session_id, seq, role, content, status, "
                "citations, tool_calls, model, kb_version, prompt_tokens, completion_tokens, "
                "latency_ms, error, created_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (message_id, session_id, row["message_count"], msg.role, msg.content, msg.status,
                 json.dumps([c.model_dump(exclude_none=True) for c in msg.citations],
                            ensure_ascii=False),
                 json.dumps(msg.tool_calls, ensure_ascii=False, default=str), msg.model,
                 msg.kb_version, msg.prompt_tokens, msg.completion_tokens, msg.latency_ms,
                 msg.error, now))
        return Message(message_id=message_id, session_id=session_id, seq=row["message_count"],
                       created_at=now, tools_used=sorted({c["name"] for c in msg.tool_calls}),
                       **msg.model_dump(include={"role", "content", "status", "citations",
                                                 "model", "kb_version", "error"}))

    async def messages(self, user_id: str, session_id: str, limit: int,
                       before_seq: int | None = None) -> list[Message]:
        sql = (f"SELECT {_MESSAGE} FROM chat_message m JOIN chat_session s "
               "ON s.session_id = m.session_id WHERE m.session_id = %s AND s.user_id = %s "
               "AND s.deleted_at IS NULL")
        params: list[Any] = [session_id, user_id]
        if before_seq is not None:
            sql += " AND m.seq < %s"
            params.append(before_seq)
        sql += " ORDER BY m.seq DESC LIMIT %s"
        params.append(limit)
        async with await self._conn() as conn:
            rows = await (await conn.execute(sql, params)).fetchall()
        return [_message(r) for r in reversed(rows)]

    async def set_feedback(self, user_id: str, message_id: str, rating: int,
                           comment: str | None) -> bool:
        async with await self._conn() as conn:
            cur = await conn.execute(
                "UPDATE chat_message SET feedback = %s, feedback_comment = %s "
                "WHERE message_id = %s AND session_id IN (SELECT session_id FROM chat_session "
                "WHERE user_id = %s AND deleted_at IS NULL)", (rating, comment, message_id,
                                                               user_id))
            return cur.rowcount == 1
