-- M13 feedback loop: confirmed VASP-cooperation outcomes + versioned
-- confidence-calibration models. Apply with:
--   psql $DATABASE_URL -f engine/store/migrations/004_feedback.sql

CREATE TABLE IF NOT EXISTS feedback_outcomes (
    id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES cases(id),
    vasp VARCHAR(128) NOT NULL,
    predicted_confidence DOUBLE PRECISION NOT NULL,
    outcome VARCHAR(16) NOT NULL,        -- confirmed|refuted|inconclusive
    notes TEXT NOT NULL DEFAULT '',
    recorded_by VARCHAR(128) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_feedback_case ON feedback_outcomes(case_id);
CREATE INDEX IF NOT EXISTS idx_feedback_outcome ON feedback_outcomes(outcome);

CREATE TABLE IF NOT EXISTS calibration_models (
    version VARCHAR(32) PRIMARY KEY,     -- cal-1, cal-2, ...
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by VARCHAR(128) NOT NULL DEFAULT '',
    n_outcomes INTEGER NOT NULL DEFAULT 0,
    bucket_values JSONB NOT NULL DEFAULT '[]',
    bucket_counts JSONB NOT NULL DEFAULT '[]'
);
