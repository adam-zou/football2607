CREATE TABLE IF NOT EXISTS titan007_handicap_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL
        CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    seq INTEGER NOT NULL CHECK (seq > 0),
    match_minute SMALLINT,
    home_score SMALLINT,
    away_score SMALLINT,
    change_time TEXT NOT NULL,
    source_status TEXT NOT NULL,
    is_suspended BOOLEAN NOT NULL,
    home_odds NUMERIC(8, 3),
    home_odds_movement TEXT
        CHECK (home_odds_movement IN ('上升', '下降', '不变')),
    handicap_raw TEXT,
    handicap_value NUMERIC(6, 2),
    handicap_movement TEXT
        CHECK (handicap_movement IN ('上升', '下降', '不变')),
    away_odds NUMERIC(8, 3),
    away_odds_movement TEXT
        CHECK (away_odds_movement IN ('上升', '下降', '不变')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id, seq),
    CHECK (
        NOT is_suspended OR (
            home_odds IS NULL
            AND home_odds_movement IS NULL
            AND handicap_raw IS NULL
            AND handicap_value IS NULL
            AND handicap_movement IS NULL
            AND away_odds IS NULL
            AND away_odds_movement IS NULL
        )
    )
);

ALTER TABLE titan007_handicap_changes
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS titan007_1x2_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL
        CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    seq INTEGER NOT NULL CHECK (seq > 0),
    match_minute SMALLINT,
    home_score SMALLINT,
    away_score SMALLINT,
    change_time TEXT NOT NULL,
    source_status TEXT NOT NULL,
    is_suspended BOOLEAN NOT NULL,
    home_win_odds NUMERIC(8, 3),
    home_win_odds_movement TEXT
        CHECK (home_win_odds_movement IN ('上升', '下降', '不变')),
    draw_odds NUMERIC(8, 3),
    draw_odds_movement TEXT
        CHECK (draw_odds_movement IN ('上升', '下降', '不变')),
    away_win_odds NUMERIC(8, 3),
    away_win_odds_movement TEXT
        CHECK (away_win_odds_movement IN ('上升', '下降', '不变')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id, seq),
    CHECK (
        NOT is_suspended OR (
            home_win_odds IS NULL
            AND home_win_odds_movement IS NULL
            AND draw_odds IS NULL
            AND draw_odds_movement IS NULL
            AND away_win_odds IS NULL
            AND away_win_odds_movement IS NULL
        )
    )
);

ALTER TABLE titan007_1x2_changes
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS titan007_over_under_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL
        CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    seq INTEGER NOT NULL CHECK (seq > 0),
    match_minute SMALLINT,
    home_score SMALLINT,
    away_score SMALLINT,
    change_time TEXT NOT NULL,
    source_status TEXT NOT NULL,
    is_suspended BOOLEAN NOT NULL,
    over_odds NUMERIC(8, 3),
    over_odds_movement TEXT
        CHECK (over_odds_movement IN ('上升', '下降', '不变')),
    total_line_raw TEXT,
    total_line_value NUMERIC(6, 2),
    total_line_movement TEXT
        CHECK (total_line_movement IN ('上升', '下降', '不变')),
    under_odds NUMERIC(8, 3),
    under_odds_movement TEXT
        CHECK (under_odds_movement IN ('上升', '下降', '不变')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id, seq),
    CHECK (
        NOT is_suspended OR (
            over_odds IS NULL
            AND over_odds_movement IS NULL
            AND total_line_raw IS NULL
            AND total_line_value IS NULL
            AND total_line_movement IS NULL
            AND under_odds IS NULL
            AND under_odds_movement IS NULL
        )
    )
);

ALTER TABLE titan007_over_under_changes
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS titan007_odds_fetch_status (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL
        CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    handicap_completed BOOLEAN NOT NULL DEFAULT FALSE,
    one_x_two_completed BOOLEAN NOT NULL DEFAULT FALSE,
    over_under_completed BOOLEAN NOT NULL DEFAULT FALSE,
    handicap_last_seq INTEGER,
    one_x_two_last_seq INTEGER,
    over_under_last_seq INTEGER,
    verification_version SMALLINT NOT NULL DEFAULT 0,
    final_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id)
);

ALTER TABLE titan007_odds_fetch_status
    ADD COLUMN IF NOT EXISTS handicap_last_seq INTEGER,
    ADD COLUMN IF NOT EXISTS one_x_two_last_seq INTEGER,
    ADD COLUMN IF NOT EXISTS over_under_last_seq INTEGER,
    ADD COLUMN IF NOT EXISTS verification_version SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS final_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

UPDATE titan007_odds_fetch_status
SET handicap_completed = FALSE,
    one_x_two_completed = FALSE,
    over_under_completed = FALSE,
    handicap_last_seq = NULL,
    one_x_two_last_seq = NULL,
    over_under_last_seq = NULL,
    final_verified_at = NULL,
    updated_at = NOW()
WHERE verification_version = 0;
