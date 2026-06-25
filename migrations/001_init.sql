-- migrations/001_init.sql
-- Full schema for a fresh installation.
-- SQLAlchemy's init_db() (create_all) handles this automatically on API startup.
-- This file is kept as a reference for DR recovery and manual inspection.

CREATE TABLE IF NOT EXISTS vm_schedules (
    vm_id               VARCHAR(255) PRIMARY KEY,
    display_name        VARCHAR(255),                   -- from naming service
    provider            VARCHAR(32)  NOT NULL,
    timezone            VARCHAR(64)  NOT NULL DEFAULT 'Australia/Sydney',

    -- 24x7 sentinel: both hours = 0 means always-on, collector skips entirely
    power_off_hour      INTEGER      NOT NULL DEFAULT 0,
    power_off_minute    INTEGER      NOT NULL DEFAULT 0,
    power_on_hour       INTEGER      NOT NULL DEFAULT 0,
    power_on_minute     INTEGER      NOT NULL DEFAULT 0,

    -- Blackout periods: {"periods": ["weekends", "christmas-shutdown", ...]}
    -- "weekends" is built-in; others are named calendar lookups in Redis.
    blackouts           JSONB        NOT NULL DEFAULT '{"periods": ["weekends"]}',

    -- All provider-specific fields in one flexible column:
    --   AWS:    {"role_arn": "...", "region": "ap-southeast-2"}
    --   Azure:  {"tenant_id": "...", "subscription_id": "...",
    --            "resource_group": "...", "vault_role": "workspace-name"}
    --   VMware: {"vcenter_host": "vcenter.internal.example.com"}
    provider_config     JSONB        NOT NULL DEFAULT '{}',

    -- Audit columns — updated by batch collector after each execution
    last_power_off_at       TIMESTAMPTZ,
    last_power_on_at        TIMESTAMPTZ,
    last_power_off_result   VARCHAR(64),
    last_power_on_result    VARCHAR(64),

    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- GIN index for provider_config lookups (subscription_id, role_arn etc.)
CREATE INDEX IF NOT EXISTS idx_vm_schedules_provider
    ON vm_schedules (provider);

CREATE INDEX IF NOT EXISTS idx_vm_schedules_provider_config
    ON vm_schedules USING gin (provider_config);


CREATE TABLE IF NOT EXISTS execution_log (
    id           BIGSERIAL    PRIMARY KEY,
    vm_id        VARCHAR(255) NOT NULL,
    display_name VARCHAR(255),
    provider     VARCHAR(32)  NOT NULL,
    action       VARCHAR(16)  NOT NULL,   -- 'on' or 'off'
    result       VARCHAR(64)  NOT NULL,   -- 'success', 'suppressed', 'error', 'expired'
    detail       TEXT,
    executed_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_execution_log_vm_id
    ON execution_log (vm_id, executed_at DESC);

CREATE INDEX IF NOT EXISTS idx_execution_log_executed_at
    ON execution_log (executed_at DESC);

-- Convenience view: latest result per VM per action
CREATE OR REPLACE VIEW vm_last_executions AS
SELECT DISTINCT ON (vm_id, action)
    vm_id, display_name, provider, action, result, detail, executed_at
FROM execution_log
ORDER BY vm_id, action, executed_at DESC;
