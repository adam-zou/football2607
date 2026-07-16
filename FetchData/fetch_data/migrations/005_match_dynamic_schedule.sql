CREATE TABLE IF NOT EXISTS match_dynamic_schedule (
    match_id BIGINT PRIMARY KEY
        REFERENCES match_status(match_id) ON DELETE CASCADE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ,
    last_succeeded_at TIMESTAMPTZ,
    last_error TEXT,
    is_abnormal BOOLEAN NOT NULL DEFAULT FALSE,
    abnormal_since TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS match_dynamic_schedule_next_attempt_idx
    ON match_dynamic_schedule (next_attempt_at);

ALTER TABLE match_status DROP COLUMN IF EXISTS final_status_checked_at;
