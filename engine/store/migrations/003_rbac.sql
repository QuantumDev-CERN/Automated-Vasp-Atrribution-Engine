-- M12 RBAC + audit trail. Apply with:
--   psql $DATABASE_URL -f engine/store/migrations/003_rbac.sql

ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS jurisdiction VARCHAR(16)
        NOT NULL DEFAULT 'IN';

CREATE TABLE IF NOT EXISTS api_users (
    id UUID PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    role VARCHAR(16) NOT NULL,          -- viewer|analyst|auditor|admin
    jurisdictions JSONB NOT NULL DEFAULT '[]',
    key_hash VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_users_key ON api_users(key_hash);

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY,
    user_id UUID,
    user_name VARCHAR(128) NOT NULL DEFAULT '',
    action VARCHAR(128) NOT NULL,
    target_type VARCHAR(32) NOT NULL DEFAULT '',
    target_id VARCHAR(128) NOT NULL DEFAULT '',
    jurisdiction VARCHAR(16) NOT NULL DEFAULT '',
    ip VARCHAR(64) NOT NULL DEFAULT '',
    outcome VARCHAR(32) NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_events(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);
