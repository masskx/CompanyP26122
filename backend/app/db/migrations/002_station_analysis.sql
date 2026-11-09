-- Fixed-station daily analysis: station configuration + batch / record / event.
-- openGauss (DBCOMPATIBILITY 'PG', PostgreSQL 9.2 dialect) and PostgreSQL compatible:
-- no identity columns, no ON CONFLICT, JSON stored as TEXT, keyword-free column names.

-- One row per station; the device directory (<= 20 devices) is a JSON list.
-- config_version increases on every change; batches keep a full snapshot.
CREATE TABLE station_config (
    station_id              VARCHAR(64)  PRIMARY KEY,
    name                    VARCHAR(200) NOT NULL,
    comm_address            VARCHAR(200),
    sampling_interval_s     INTEGER      NOT NULL CHECK (sampling_interval_s IN (30, 60, 300)),
    calibration_factor      DOUBLE PRECISION NOT NULL DEFAULT 1,
    dose_l1                 DOUBLE PRECISION,           -- µGy/h, Level 1
    dose_l2                 DOUBLE PRECISION,           -- µGy/h, Level 2
    battery_warn_v          DOUBLE PRECISION,
    battery_alarm_v         DOUBLE PRECISION,
    battery_drop_v          DOUBLE PRECISION,
    battery_drop_minutes    INTEGER,
    battery_charge_minutes  INTEGER,
    has_rain_gauge          BOOLEAN      NOT NULL DEFAULT FALSE,
    enabled                 BOOLEAN      NOT NULL DEFAULT TRUE,
    devices                 TEXT         NOT NULL,     -- JSON [{device_id, name, enabled, location}]
    config_version          INTEGER      NOT NULL DEFAULT 1,
    updated_at              TIMESTAMP WITH TIME ZONE NOT NULL
);

-- One analysis run of one calendar day. (analysis_date, version) is the idempotency key;
-- a forced manual re-run adds a revision. The current result of a day is the highest
-- revision that succeeded or is partial.
CREATE TABLE analysis_batch (
    batch_id         VARCHAR(64)  PRIMARY KEY,
    analysis_date    DATE         NOT NULL,
    version          VARCHAR(32)  NOT NULL,
    revision         INTEGER      NOT NULL,
    status           VARCHAR(16)  NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    trigger_type     VARCHAR(16)  NOT NULL CHECK (trigger_type IN ('schedule', 'catchup', 'manual', 'retry')),
    attempts         INTEGER      NOT NULL DEFAULT 0,
    time_zone        VARCHAR(64)  NOT NULL,
    window_start     TIMESTAMP WITH TIME ZONE NOT NULL,
    window_end       TIMESTAMP WITH TIME ZONE NOT NULL,
    config_snapshot  TEXT,        -- JSON: stations, thresholds, calibration, devices, params
    counts           TEXT,        -- JSON
    config_gaps      TEXT,        -- JSON list
    errors           TEXT,        -- JSON list (failed stations / devices)
    created_at       TIMESTAMP WITH TIME ZONE NOT NULL,
    started_at       TIMESTAMP WITH TIME ZONE,
    finished_at      TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_analysis_batch UNIQUE (analysis_date, version, revision)
);
CREATE INDEX ix_analysis_batch_date ON analysis_batch (analysis_date, version);

-- One per abnormal device (detector) or station battery (backup_power).
CREATE TABLE analysis_record (
    record_id           VARCHAR(40)  PRIMARY KEY,
    batch_id            VARCHAR(64)  NOT NULL REFERENCES analysis_batch (batch_id),
    analysis_date       DATE         NOT NULL,
    station_id          VARCHAR(64)  NOT NULL,
    station_name        VARCHAR(200) NOT NULL,
    equipment_type      VARCHAR(16)  NOT NULL CHECK (equipment_type IN ('detector', 'backup_power')),
    device_id           VARCHAR(64)  NOT NULL,
    device_name         VARCHAR(200) NOT NULL,
    level_code          VARCHAR(20)  NOT NULL CHECK (level_code IN ('watch', 'inspect', 'insufficient_data')),
    title               VARCHAR(1000) NOT NULL,
    sources             VARCHAR(200) NOT NULL,     -- JSON list
    event_count         INTEGER      NOT NULL,
    data_status         VARCHAR(16)  NOT NULL CHECK (data_status IN ('complete', 'partial', 'missing')),
    day_stats           TEXT,                      -- JSON
    explanation         TEXT,                      -- JSON (summary, evidence, recommendations ...)
    explanation_method  VARCHAR(20),               -- llm | template | template_fallback
    confidence          DOUBLE PRECISION,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_analysis_record UNIQUE (batch_id, station_id, device_id)
);
CREATE INDEX ix_analysis_record_batch ON analysis_record (batch_id, level_code);
CREATE INDEX ix_analysis_record_device ON analysis_record (station_id, device_id, analysis_date);

-- One abnormal interval of a record.
CREATE TABLE analysis_event (
    event_id                VARCHAR(40)  PRIMARY KEY,
    record_id               VARCHAR(40)  NOT NULL REFERENCES analysis_record (record_id),
    code                    VARCHAR(8)   NOT NULL,   -- E1, E2 ... within the record
    kind                    VARCHAR(40)  NOT NULL,
    title                   VARCHAR(200) NOT NULL,
    level_code              VARCHAR(20)  NOT NULL,
    source_type             VARCHAR(20)  NOT NULL CHECK (source_type IN ('source_reported', 'rule', 'data_quality')),
    category                VARCHAR(20)  NOT NULL,
    start_time              TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time                TIMESTAMP WITH TIME ZONE NOT NULL,
    duration_s              DOUBLE PRECISION NOT NULL,
    open_at_start           BOOLEAN      NOT NULL,
    open_at_end             BOOLEAN      NOT NULL,
    sample_count            INTEGER      NOT NULL,
    expected_samples        INTEGER      NOT NULL,
    completeness            DOUBLE PRECISION,
    unit                    VARCHAR(16),
    min_value               DOUBLE PRECISION,
    max_value               DOUBLE PRECISION,
    mean_value              DOUBLE PRECISION,
    std_value               DOUBLE PRECISION,
    corroborated_by_source  BOOLEAN,
    rule_info               TEXT,        -- JSON: rule id, description, thresholds
    comparisons             TEXT,        -- JSON
    context                 TEXT,        -- JSON: AC power, rainfall, shelter
    value_series            TEXT,        -- JSON [[time, value], ...] within the interval
    values_truncated        BOOLEAN      NOT NULL DEFAULT FALSE,
    CONSTRAINT uq_analysis_event UNIQUE (record_id, code)
);
CREATE INDEX ix_analysis_event_record ON analysis_event (record_id);
