-- M10 watchlist schema. Apply with:
--   psql $DATABASE_URL -f engine/store/migrations/002_watchlist.sql
-- (docker-compose postgres: psql postgresql://vasp:vasp@localhost:5432/vasp)
-- Note: init_store() runs Base.metadata.create_all, so fresh deploys
-- don't need this file — it's for existing databases.

CREATE TABLE IF NOT EXISTS watches (
    id UUID PRIMARY KEY,
    address VARCHAR(128) NOT NULL,
    chain VARCHAR(32) NOT NULL,
    label VARCHAR(128) NOT NULL DEFAULT '',
    case_id UUID REFERENCES cases(id),
    alert_url VARCHAR(512) NOT NULL DEFAULT '',
    created_by VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    seen_hashes JSONB NOT NULL DEFAULT '[]',
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_watches_status ON watches(status);
CREATE INDEX IF NOT EXISTS idx_watches_address ON watches(chain, address);

CREATE TABLE IF NOT EXISTS watch_alerts (
    id UUID PRIMARY KEY,
    watch_id UUID NOT NULL REFERENCES watches(id),
    tx_hash VARCHAR(128) NOT NULL,
    direction VARCHAR(8) NOT NULL,
    counterparty VARCHAR(128) NOT NULL,
    value VARCHAR(64) NOT NULL DEFAULT '',
    asset VARCHAR(64) NOT NULL DEFAULT '',
    vasp_hit VARCHAR(128),
    delivered BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_watch_alerts_watch ON watch_alerts(watch_id);
