-- openGauss on the compute server stores only conversations and the work-order copy.
-- openGauss (PostgreSQL 9.2 dialect) and PostgreSQL compatible; JSON stored as TEXT.

-- Tables of version 0.3 (own analysis batches and station configuration) are no longer used.
DROP TABLE IF EXISTS analysis_event;
DROP TABLE IF EXISTS analysis_record;
DROP TABLE IF EXISTS analysis_batch;
DROP TABLE IF EXISTS station_config;

-- Copy of the main server's internal maintenance work orders (pushed by the main server).
CREATE TABLE maintenance_work_order (
    event_id           VARCHAR(64)  PRIMARY KEY,
    site_id            VARCHAR(64)  NOT NULL,
    equipment_id       VARCHAR(64)  NOT NULL,
    risk_type          VARCHAR(20)  NOT NULL CHECK (risk_type IN ('detector', 'backup_power', 'general')),
    health_level       VARCHAR(20)  NOT NULL CHECK (health_level IN ('watch', 'inspect', 'insufficient_data')),
    status             VARCHAR(20)  CHECK (status IN ('pending', 'confirmed')),
    triggered_at       TIMESTAMP WITH TIME ZONE NOT NULL,
    analysis_id        VARCHAR(64),
    ai_result          TEXT,        -- JSON: explanation, findings, actions, limitations, versions
    review             TEXT,        -- JSON: outcome, comment, reviewed_at (no reviewer identity)
    source_updated_at  TIMESTAMP WITH TIME ZONE,   -- main server's last change
    received_at        TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX ix_mwo_equipment ON maintenance_work_order (site_id, equipment_id, triggered_at);

-- Assistant: which preset workflow a conversation belongs to (NULL = free chat).
ALTER TABLE chat_session ADD COLUMN workflow VARCHAR(40);

-- Assistant: generated monitoring reports (part of the conversation; files on disk).
CREATE TABLE assistant_report (
    report_id    VARCHAR(40)  PRIMARY KEY,
    user_id      VARCHAR(128) NOT NULL,
    session_id   VARCHAR(40),
    title        VARCHAR(200) NOT NULL,
    params       TEXT         NOT NULL,   -- JSON: stations, window, metrics, unit, decimals, granularity
    file_name    VARCHAR(200) NOT NULL,
    size_bytes   INTEGER      NOT NULL,
    row_count    INTEGER      NOT NULL,
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    deleted_at   TIMESTAMP WITH TIME ZONE
);
CREATE INDEX ix_assistant_report_user ON assistant_report (user_id, created_at);
