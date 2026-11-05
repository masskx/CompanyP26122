-- Conversation storage. Written for openGauss (PostgreSQL 9.2 dialect) and PostgreSQL:
-- no identity columns, no ON CONFLICT, JSON kept in TEXT columns.
-- The database must be created with DBCOMPATIBILITY 'PG' (in the default 'A' mode an
-- empty string is stored as NULL).

-- End users, keyed by identity provider (issuer) + subject. user_id is internal.
CREATE TABLE app_users (
    user_id        VARCHAR(40)  PRIMARY KEY,
    issuer         VARCHAR(128) NOT NULL,
    subject        VARCHAR(256) NOT NULL,
    display_name   VARCHAR(256),
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    last_seen_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_app_users_identity UNIQUE (issuer, subject)
);

CREATE TABLE chat_sessions (
    session_id     VARCHAR(40)  PRIMARY KEY,
    user_id        VARCHAR(40)  NOT NULL REFERENCES app_users (user_id),
    title          VARCHAR(200),
    message_count  INTEGER      NOT NULL DEFAULT 0,  -- also the per-session seq counter
    archived       BOOLEAN      NOT NULL DEFAULT FALSE,
    deleted_at     TIMESTAMP WITH TIME ZONE,          -- soft delete (audit trail kept)
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at     TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX ix_chat_sessions_user_updated ON chat_sessions (user_id, updated_at);

CREATE TABLE chat_messages (
    message_id               VARCHAR(40)  PRIMARY KEY,
    session_id               VARCHAR(40)  NOT NULL REFERENCES chat_sessions (session_id),
    seq                      INTEGER      NOT NULL,
    role                     VARCHAR(16)  NOT NULL CHECK (role IN ('user', 'assistant')),
    content                  TEXT         NOT NULL,
    status                   VARCHAR(16)  NOT NULL CHECK (status IN ('complete', 'error', 'interrupted')),
    knowledge_index_version  VARCHAR(128),
    model_json               TEXT,        -- provider / name / precision / revision
    tools_used               VARCHAR(512),-- comma-separated tool names
    tool_trace_json          TEXT,        -- tool calls + arguments, for audit
    prompt_tokens            INTEGER,
    completion_tokens        INTEGER,
    latency_ms               INTEGER,
    error_code               VARCHAR(64),
    created_at               TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_chat_messages_session_seq UNIQUE (session_id, seq)
);

CREATE TABLE message_citations (
    citation_id    VARCHAR(40)  PRIMARY KEY,
    message_id     VARCHAR(40)  NOT NULL REFERENCES chat_messages (message_id),
    ref_no         INTEGER      NOT NULL,
    kind           VARCHAR(16)  NOT NULL,
    title          VARCHAR(512) NOT NULL,
    source         VARCHAR(512),
    doc_id         VARCHAR(128),
    chunk_id       VARCHAR(160),
    section        VARCHAR(512),
    page           INTEGER,
    kb_version     VARCHAR(128),
    score          DOUBLE PRECISION,
    equipment_id   VARCHAR(128),
    time_range     VARCHAR(128),
    snippet        TEXT
);
CREATE INDEX ix_message_citations_message ON message_citations (message_id);

CREATE TABLE message_feedback (
    feedback_id    VARCHAR(40)  PRIMARY KEY,
    message_id     VARCHAR(40)  NOT NULL REFERENCES chat_messages (message_id),
    user_id        VARCHAR(40)  NOT NULL REFERENCES app_users (user_id),
    rating         SMALLINT     NOT NULL CHECK (rating IN (-1, 1)),
    comment        TEXT,
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_message_feedback UNIQUE (message_id, user_id)
);
