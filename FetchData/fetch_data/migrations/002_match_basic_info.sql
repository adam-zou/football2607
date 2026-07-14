CREATE TABLE IF NOT EXISTS match_basic_info (
    match_id BIGINT PRIMARY KEY REFERENCES match_status(match_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    league TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ,
    home_score SMALLINT,
    away_score SMALLINT,
    status_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE match_basic_info
    ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

UPDATE match_basic_info
SET scheduled_at = scheduled_time::TIMESTAMP AT TIME ZONE 'Asia/Shanghai'
WHERE scheduled_at IS NULL
  AND scheduled_time ~ '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$';
