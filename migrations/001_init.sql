-- migrations/001_init.sql
-- Run once on a fresh database, or let SQLAlchemy's create_all handle it.
-- Kept here for reference, auditing, and manual DR recovery.

CREATE TABLE IF NOT EXISTS vm_schedules (
    vm_id               VARCHAR(255) PRIMARY KEY,
    provider            VARCHAR(32)  NOT NULL,
    timezone            VARCHAR(64)  NOT NULL DEFAULT 'Australia/Sydney',

    power_off_hour      INTEGER      NOT NULL,
    power_off_minute    INTEGER      NOT NULL DEFAULT 0,
    power_on_hour       INTEGER      NOT NULL,
    power_on_minute     INTEGER      NOT NULL DEFAULT 0,
    -- blackouts: {"periods": ["weekends", "christmas-shutdown", "easter-break", ...]}
    -- "weekends" is a built-in period. Empty array = no blackouts.
    -- Both power hours = 0 is the 24x7 sentinel (no operations scheduled).
    blackouts           JSONB        NOT NULL DEFAULT '{"periods": ["weekends", "christmas-shutdown", "easter-break"]}',

    -- AWS
    region              VARCHAR(64),
    role_arn            VARCHAR(255),

    -- Azure
    subscription_id     VARCHAR(64),
    resource_group      VARCHAR(128),

    -- VMware
    vcenter_host        VARCHAR(255),

    -- Audit columns — updated by batch collector after each run
    last_power_off_at       TIMESTAMPTZ,
    last_power_on_at        TIMESTAMPTZ,
    last_power_off_result   VARCHAR(64),
    last_power_on_result    VARCHAR(64),

    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Collector query: fetch all rows — no WHERE clause needed at 5k rows.
-- Index retained for future Option B timezone-pushdown queries if fleet grows.
CREATE INDEX IF NOT EXISTS idx_vm_schedules_provider
    ON vm_schedules (provider);

CREATE INDEX IF NOT EXISTS idx_vm_schedules_timezone_hours
    ON vm_schedules (timezone, power_on_hour, power_on_minute, power_off_hour, power_off_minute);


CREATE TABLE IF NOT EXISTS execution_log (
    id          BIGSERIAL    PRIMARY KEY,
    vm_id       VARCHAR(255) NOT NULL,
    provider    VARCHAR(32)  NOT NULL,
    action      VARCHAR(16)  NOT NULL,   -- 'on' or 'off'
    result      VARCHAR(64)  NOT NULL,   -- 'success', 'suppressed', 'error'
    detail      TEXT,                    -- error message or suppression reason
    executed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_execution_log_vm_id
    ON execution_log (vm_id, executed_at DESC);

CREATE INDEX IF NOT EXISTS idx_execution_log_executed_at
    ON execution_log (executed_at DESC);

-- Useful view: latest result per VM
CREATE OR REPLACE VIEW vm_last_executions AS
SELECT DISTINCT ON (vm_id, action)
    vm_id, provider, action, result, detail, executed_at
FROM execution_log
ORDER BY vm_id, action, executed_at DESC;
