CREATE TABLE IF NOT EXISTS titan007_odds_market_schedule (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL
        CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    market TEXT NOT NULL
        CHECK (market IN ('handicap', 'one_x_two', 'over_under')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ,
    last_succeeded_at TIMESTAMPTZ,
    last_error TEXT,
    is_abnormal BOOLEAN NOT NULL DEFAULT FALSE,
    abnormal_since TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id, market)
);

-- Upgrade databases that used the former one-row-per-match schedule. Preserve
-- its current cadence/backoff for all 18 pages, then let future outcomes diverge.
DO $$
BEGIN
    IF to_regclass('titan007_odds_schedule') IS NOT NULL THEN
        EXECUTE $migration$
            INSERT INTO titan007_odds_market_schedule (
                match_id, company_id, market, consecutive_failures,
                next_attempt_at, last_attempt_at, last_succeeded_at, last_error,
                is_abnormal, abnormal_since, created_at, updated_at
            )
            SELECT schedule.match_id,
                   company.company_id,
                   market.market,
                   schedule.consecutive_failures,
                   schedule.next_attempt_at,
                   schedule.last_attempt_at,
                   schedule.last_succeeded_at,
                   schedule.last_error,
                   schedule.is_abnormal,
                   schedule.abnormal_since,
                   schedule.created_at,
                   schedule.updated_at
            FROM titan007_odds_schedule AS schedule
            CROSS JOIN (
                VALUES (3), (4), (8), (24), (31), (47)
            ) AS company(company_id)
            CROSS JOIN (
                VALUES ('handicap'), ('one_x_two'), ('over_under')
            ) AS market(market)
            ON CONFLICT (match_id, company_id, market) DO NOTHING
        $migration$;
    END IF;
END
$$;

-- `fetch-odds` may initialize odds tables before the continuous synchronizer has
-- created match_status. Add the relationship whenever both tables are available.
DO $$
BEGIN
    IF to_regclass('match_status') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM pg_constraint
           WHERE conname = 'titan007_odds_market_schedule_match_id_fkey'
       )
    THEN
        ALTER TABLE titan007_odds_market_schedule
            ADD CONSTRAINT titan007_odds_market_schedule_match_id_fkey
            FOREIGN KEY (match_id) REFERENCES match_status(match_id)
            ON DELETE CASCADE;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS titan007_odds_market_schedule_next_attempt_idx
    ON titan007_odds_market_schedule (next_attempt_at);
