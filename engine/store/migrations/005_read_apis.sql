-- M26 read-API schema. Apply with:
--   psql $DATABASE_URL -f engine/store/migrations/005_read_apis.sql
-- (docker-compose postgres: psql postgresql://vasp:vasp@localhost:5432/vasp)
--
-- Fresh databases get all of this via Base.metadata.create_all; this file
-- is only needed to upgrade a database created before M26. Every
-- statement is idempotent (IF NOT EXISTS).

-- Trace outcome summary on reports (no more parsing report_text).
ALTER TABLE reports
    ADD COLUMN IF NOT EXISTS risk_score INTEGER,
    ADD COLUMN IF NOT EXISTS risk_level VARCHAR(16) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS terminal_address VARCHAR(128),
    ADD COLUMN IF NOT EXISTS terminal_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS hop_count INTEGER;

-- Operator-set watch classification (free text, never engine-inferred).
ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS classification VARCHAR(64) NOT NULL DEFAULT '';

-- Analyst alert dispositions.
ALTER TABLE watch_alerts
    ADD COLUMN IF NOT EXISTS disposition VARCHAR(32) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS disposition_notes TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS disposition_by VARCHAR(128) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS disposition_at TIMESTAMPTZ;

-- Operator contact on API users.
ALTER TABLE api_users
    ADD COLUMN IF NOT EXISTS email VARCHAR(256) NOT NULL DEFAULT '';

-- One row per watch-check cycle (the watch detail history).
CREATE TABLE IF NOT EXISTS watch_checks (
    id UUID PRIMARY KEY,
    watch_id UUID NOT NULL REFERENCES watches(id),
    checked_at TIMESTAMPTZ NOT NULL,
    txs_seen INTEGER NOT NULL DEFAULT 0,
    new_events INTEGER NOT NULL DEFAULT 0,
    alerts_delivered INTEGER NOT NULL DEFAULT 0,
    baseline BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_watch_checks_watch
    ON watch_checks(watch_id, checked_at DESC);

-- Durable register of attribution filings with authorities.
CREATE TABLE IF NOT EXISTS filings (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES cases(id),
    report_id UUID NOT NULL REFERENCES reports(id),
    channel VARCHAR(32) NOT NULL DEFAULT 'sahyog',
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    ack_ref VARCHAR(256) NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    filed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_filings_filed ON filings(filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_filings_case ON filings(case_id);
