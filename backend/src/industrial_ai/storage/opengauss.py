"""openGauss implementation of ConversationStore (psycopg 3, plain SQL).

openGauss speaks the PostgreSQL wire protocol (server_version 9.2.x). Notes:
* Use a non-initial user and md5-compatible authentication; the initial user cannot
  connect remotely and libpq does not support openGauss's sha256 method.
* The database must use DBCOMPATIBILITY 'PG'; `migrate()` refuses an 'A' database.
* SQL is limited to the 9.2 dialect (no ON CONFLICT, no identity columns).
"""

import json
import logging
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from industrial_ai.core.identity import Principal
from industrial_ai.schemas.analysis import ModelInfo
from industrial_ai.schemas.assistant import ChatMessage, ChatSession, Citation
from industrial_ai.storage.base import StorageUnavailableError
from industrial_ai.storage.conversations import NewMessage, UserRecord, new_id

logger = logging.getLogger(__name__)

_SESSION_COLS = "session_id, title, message_count, archived, created_at, updated_at"
_MESSAGE_COLS = ("m.message_id, m.session_id, m.seq, m.role, m.content, m.status, m.created_at, "
                 "m.knowledge_index_version, m.model_json, m.tools_used, m.error_code")


def _session(row: dict) -> ChatSession:
    return ChatSession(**row)


def _message(row: dict, citations: list[Citation]) -> ChatMessage:
    model = row.pop("model_json")
    tools = row.pop("tools_used")
    return ChatMessage(**row, citations=citations,
                       model=ModelInfo.model_validate_json(model) if model else None,
                       tools_used=tools.split(",") if tools else [])


class DatabaseCompatibilityError(RuntimeError):
    pass


async def migrate(dsn: str) -> list[str]:
    """Apply pending migrations in order, each in its own transaction. Returns applied names."""
    applied: list[str] = []
    async with await AsyncConnection.connect(dsn, autocommit=False) as conn:
        compat = await _compatibility(conn)
        if compat not in (None, "PG"):
            raise DatabaseCompatibilityError(
                f"database compatibility is {compat!r}; create the database with "
                "DBCOMPATIBILITY 'PG' (in 'A' mode empty strings become NULL)")
        async with conn.transaction():
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version VARCHAR(64) PRIMARY KEY,"
                " applied_at TIMESTAMP WITH TIME ZONE NOT NULL)")
        done = {r[0] for r in await (await conn.execute(
            "SELECT version FROM schema_migrations")).fetchall()}
        files = sorted(f for f in resources.files("industrial_ai.storage.migrations").iterdir()
                       if f.name.endswith(".sql"))
        for f in files:
            version = f.name.removesuffix(".sql")
            if version in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text(encoding="utf-8"))  # type: ignore[arg-type]
                await conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (version, datetime.now(UTC)))
            applied.append(version)
            logger.info("migration applied", extra={"fields": {"version": version}})
    return applied


async def _compatibility(conn: AsyncConnection) -> str | None:
    """openGauss database compatibility mode, or None on PostgreSQL."""
    try:
        async with conn.transaction():
            row = await (await conn.execute(
                "SELECT datcompatibility FROM pg_database WHERE datname = current_database()"
            )).fetchone()
        return row[0] if row else None
    except Exception:  # column does not exist on PostgreSQL
        return None


class OpenGaussConversationStore:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._pool = AsyncConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=False,
            kwargs={"row_factory": dict_row, "autocommit": False},
        )
        self._opened = False

    async def _ensure_open(self) -> None:
        if not self._opened:
            await self._pool.open(wait=False)
            self._opened = True

    def _conn(self) -> Any:
        return self._pool.connection()

    async def ping(self) -> None:
        try:
            await self._ensure_open()
            async with self._conn() as conn:
                await conn.execute("SELECT 1")
        except Exception as exc:
            raise StorageUnavailableError("conversation database unreachable") from exc

    async def close(self) -> None:
        if self._opened:
            await self._pool.close()
            self._opened = False

    # ---- users ------------------------------------------------------------------------

    async def ensure_user(self, principal: Principal) -> UserRecord:
        await self._ensure_open()
        now = datetime.now(UTC)
        for _ in range(2):  # retry once if a concurrent request inserted the same user
            try:
                async with self._conn() as conn, conn.transaction():
                    row = await (await conn.execute(
                        "SELECT user_id, created_at, display_name FROM app_users "
                        "WHERE issuer = %s AND subject = %s",
                        (principal.issuer, principal.subject))).fetchone()
                    if row:
                        name = principal.display_name or row["display_name"]
                        await conn.execute(
                            "UPDATE app_users SET last_seen_at = %s, display_name = %s "
                            "WHERE user_id = %s", (now, name, row["user_id"]))
                        return UserRecord(row["user_id"], principal.issuer, principal.subject,
                                          name, row["created_at"], now)
                    user_id = new_id("usr")
                    await conn.execute(
                        "INSERT INTO app_users (user_id, issuer, subject, display_name, "
                        "created_at, last_seen_at) VALUES (%s, %s, %s, %s, %s, %s)",
                        (user_id, principal.issuer, principal.subject, principal.display_name,
                         now, now))
                    return UserRecord(user_id, principal.issuer, principal.subject,
                                      principal.display_name, now, now)
            except UniqueViolation:
                continue
        raise StorageUnavailableError("could not register user")

    # ---- sessions ---------------------------------------------------------------------

    async def create_session(self, user_id: str, title: str | None) -> ChatSession:
        await self._ensure_open()
        now = datetime.now(UTC)
        session_id = new_id("cs")
        async with self._conn() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)", (session_id, user_id, title, now, now))
        return ChatSession(session_id=session_id, title=title, message_count=0, archived=False,
                           created_at=now, updated_at=now)

    async def get_session(self, user_id: str, session_id: str) -> ChatSession | None:
        await self._ensure_open()
        async with self._conn() as conn:
            row = await (await conn.execute(
                f"SELECT {_SESSION_COLS} FROM chat_sessions "
                "WHERE session_id = %s AND user_id = %s AND deleted_at IS NULL",
                (session_id, user_id))).fetchone()
        return _session(row) if row else None

    async def list_sessions(self, user_id: str, *, limit: int, before: datetime | None,
                            include_archived: bool) -> list[ChatSession]:
        await self._ensure_open()
        sql = (f"SELECT {_SESSION_COLS} FROM chat_sessions "
               "WHERE user_id = %s AND deleted_at IS NULL")
        params: list[Any] = [user_id]
        if not include_archived:
            sql += " AND archived = FALSE"
        if before is not None:
            sql += " AND updated_at < %s"
            params.append(before)
        sql += " ORDER BY updated_at DESC, session_id DESC LIMIT %s"
        params.append(limit)
        async with self._conn() as conn:
            rows = await (await conn.execute(sql, params)).fetchall()
        return [_session(r) for r in rows]

    async def update_session(self, user_id: str, session_id: str, *, title: str | None,
                             archived: bool | None) -> ChatSession | None:
        await self._ensure_open()
        sets, params = ["updated_at = %s"], [datetime.now(UTC)]
        if title is not None:
            sets.append("title = %s")
            params.append(title)
        if archived is not None:
            sets.append("archived = %s")
            params.append(archived)
        async with self._conn() as conn, conn.transaction():
            row = await (await conn.execute(
                f"UPDATE chat_sessions SET {', '.join(sets)} "
                "WHERE session_id = %s AND user_id = %s AND deleted_at IS NULL "
                f"RETURNING {_SESSION_COLS}", [*params, session_id, user_id])).fetchone()
        return _session(row) if row else None

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        await self._ensure_open()
        async with self._conn() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE chat_sessions SET deleted_at = %s "
                "WHERE session_id = %s AND user_id = %s AND deleted_at IS NULL",
                (datetime.now(UTC), session_id, user_id))
            return cur.rowcount == 1

    # ---- messages ---------------------------------------------------------------------

    async def add_message(self, user_id: str, message: NewMessage) -> ChatMessage:
        await self._ensure_open()
        now = datetime.now(UTC)
        message_id = new_id("msg")
        async with self._conn() as conn, conn.transaction():
            # Atomically allocate the next seq (row lock on the session) and check ownership.
            row = await (await conn.execute(
                "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = %s "
                "WHERE session_id = %s AND user_id = %s AND deleted_at IS NULL "
                "RETURNING message_count", (now, message.session_id, user_id))).fetchone()
            if row is None:
                raise KeyError(message.session_id)
            seq = row["message_count"]
            await conn.execute(
                "INSERT INTO chat_messages (message_id, session_id, seq, role, content, status, "
                "knowledge_index_version, model_json, tools_used, tool_trace_json, prompt_tokens,"
                " completion_tokens, latency_ms, error_code, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (message_id, message.session_id, seq, message.role, message.content,
                 message.status, message.knowledge_index_version,
                 message.model.model_dump_json() if message.model else None,
                 ",".join(message.tools_used) or None,
                 json.dumps(message.tool_trace, ensure_ascii=False) if message.tool_trace
                 else None,
                 message.prompt_tokens, message.completion_tokens, message.latency_ms,
                 message.error_code, now))
            for c in message.citations:
                await conn.execute(
                    "INSERT INTO message_citations (citation_id, message_id, ref_no, kind, title,"
                    " source, doc_id, chunk_id, section, page, kb_version, score, equipment_id,"
                    " time_range, snippet) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (new_id("cit"), message_id, c.ref, c.kind, c.title, c.source, c.doc_id,
                     c.chunk_id, c.section, c.page, c.kb_version, c.score, c.equipment_id,
                     c.time_range, c.snippet))
        return ChatMessage(
            message_id=message_id, session_id=message.session_id, seq=seq,
            role=message.role, content=message.content, status=message.status,  # type: ignore[arg-type]
            created_at=now, citations=message.citations,
            knowledge_index_version=message.knowledge_index_version, model=message.model,
            tools_used=message.tools_used, error_code=message.error_code,
        )

    async def list_messages(self, user_id: str, session_id: str, *, limit: int,
                            before_seq: int | None) -> list[ChatMessage]:
        await self._ensure_open()
        sql = (f"SELECT {_MESSAGE_COLS} FROM chat_messages m "
               "JOIN chat_sessions s ON s.session_id = m.session_id "
               "WHERE m.session_id = %s AND s.user_id = %s AND s.deleted_at IS NULL")
        params: list[Any] = [session_id, user_id]
        if before_seq is not None:
            sql += " AND m.seq < %s"
            params.append(before_seq)
        sql += " ORDER BY m.seq DESC LIMIT %s"
        params.append(limit)
        async with self._conn() as conn:
            rows = list(reversed(await (await conn.execute(sql, params)).fetchall()))
            citations: dict[str, list[Citation]] = {r["message_id"]: [] for r in rows}
            if citations:
                cit_rows = await (await conn.execute(
                    "SELECT message_id, ref_no AS ref, kind, title, source, doc_id, chunk_id, "
                    "section, page, kb_version, score, equipment_id, time_range, snippet "
                    "FROM message_citations WHERE message_id = ANY(%s) ORDER BY ref_no",
                    (list(citations),))).fetchall()
                for c in cit_rows:
                    citations[c.pop("message_id")].append(Citation(**c))
        return [_message(r, citations[r["message_id"]]) for r in rows]

    async def add_feedback(self, user_id: str, message_id: str, rating: int,
                           comment: str | None) -> bool:
        await self._ensure_open()
        async with self._conn() as conn, conn.transaction():
            owned = await (await conn.execute(
                "SELECT 1 FROM chat_messages m JOIN chat_sessions s "
                "ON s.session_id = m.session_id "
                "WHERE m.message_id = %s AND s.user_id = %s AND s.deleted_at IS NULL",
                (message_id, user_id))).fetchone()
            if owned is None:
                return False
            # Portable upsert: replace any previous rating by this user.
            await conn.execute(
                "DELETE FROM message_feedback WHERE message_id = %s AND user_id = %s",
                (message_id, user_id))
            await conn.execute(
                "INSERT INTO message_feedback (feedback_id, message_id, user_id, rating, comment,"
                " created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (new_id("fb"), message_id, user_id, rating, comment, datetime.now(UTC)))
        return True
