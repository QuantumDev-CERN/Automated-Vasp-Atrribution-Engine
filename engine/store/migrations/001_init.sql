-- M7 initial schema. Apply with:
--   psql $DATABASE_URL -f engine/store/migrations/001_init.sql
-- (docker-compose postgres: psql postgresql://vasp:vasp@localhost:5432/vasp)

CREATE TABLE IF NOT EXISTS cases (
    id UUID PRIMARY KEY,
    fir_number VARCHAR(64) NOT NULL,
    suspect_address VARCHAR(128) NOT NULL,
    chain VARCHAR(32) NOT NULL,
    officer_id VARCHAR(64) NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'received',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trace_jobs (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES cases(id),
    address VARCHAR(128) NOT NULL,
    chain VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    arq_job_id VARCHAR(64),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trace_jobs_case ON trace_jobs(case_id);

CREATE TABLE IF NOT EXISTS reports (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES trace_jobs(id),
    case_id UUID NOT NULL REFERENCES cases(id),
    report_text TEXT NOT NULL,
    report_hash VARCHAR(64) NOT NULL UNIQUE,
    inputs_hash VARCHAR(64) NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    engine_version VARCHAR(64) NOT NULL DEFAULT 'unknown',
    certificate_statement TEXT NOT NULL,
    webhook_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reports_job ON reports(job_id);
