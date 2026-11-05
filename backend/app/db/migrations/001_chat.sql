-- Agent conversations. openGauss (PostgreSQL 9.2 dialect) and PostgreSQL compatible:
-- no identity columns, no ON CONFLICT, JSON stored as TEXT.
-- The database must use DBCOMPATIBILITY 'PG' (scripts/init_db.py --admin-dsn creates it).
--
-- user_id is the identifier of the end user. Until the client application provides login,
-- it is 'anonymous' (or an unauthenticated X-User-Id header). When the client's user table
-- arrives, user_id holds that table's key; add a foreign key or a users table then.

CREATE TABLE chat_session (
    session_id     VARCHAR(40)  PRIMARY KEY,
    user_id        VARCHAR(128) NOT NULL,
    title          VARCHAR(200),
    message_count  INTEGER      NOT NULL DEFAULT 0,   -- also the per-session seq counter
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    deleted_at     TIMESTAMP WITH TIME ZONE              -- soft delete
);
CREATE INDEX ix_chat_session_user ON chat_session (user_id, updated_at);

CREATE TABLE chat_message (
    message_id         VARCHAR(40)  PRIMARY KEY,
    session_id         VARCHAR(40)  NOT NULL REFERENCES chat_session (session_id),
    seq                INTEGER      NOT NULL,
    role               VARCHAR(16)  NOT NULL CHECK (role IN ('user', 'assistant')),
    content            TEXT         NOT NULL,
    status             VARCHAR(16)  NOT NULL CHECK (status IN ('complete', 'error', 'interrupted')),
    citations          TEXT,        -- JSON list of sources the answer cites
    tool_calls         TEXT,        -- JSON list of tool calls (audit)
    model              VARCHAR(128),
    kb_version         VARCHAR(128),
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    latency_ms         INTEGER,
    error              VARCHAR(256),
    feedback           SMALLINT     CHECK (feedback IN (-1, 1)),
    feedback_comment   TEXT,
    created_at         TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_chat_message_seq UNIQUE (session_id, seq)
);
