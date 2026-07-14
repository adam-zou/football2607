CREATE TABLE IF NOT EXISTS match_basic_info (
    match_id BIGINT PRIMARY KEY REFERENCES match_status(match_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    league TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    home_score SMALLINT,
    away_score SMALLINT,
    status_text TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
