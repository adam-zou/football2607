-- PostgreSQL
--
-- Create three live views for the MatchWeb odds-filter rules:
--   1. public.match_odds_filter_hits: every qualifying historical odds row.
--   2. public.match_odds_filter_summary: matches where at least one odds
--      category is hit by three or more companies.
--   3. public.match_odds_filter_market_summary: one row per match and odds
--      category hit by three or more companies, with the largest line value
--      and its score-based Asian-market result.
--
-- This script intentionally does not filter by match date or match status.
-- It can be run repeatedly.

CREATE OR REPLACE VIEW public.match_odds_filter_hits AS
WITH company_three_matches AS (
    -- Match-level prerequisite: company 3 has data in at least one market.
    SELECT match_id
    FROM public.titan007_handicap_changes
    WHERE company_id = 3

    UNION

    SELECT match_id
    FROM public.titan007_1x2_changes
    WHERE company_id = 3

    UNION

    SELECT match_id
    FROM public.titan007_over_under_changes
    WHERE company_id = 3
),
filter_hits AS (
    -- Handicap: home odds below 0.700.
    SELECT
        match_id,
        company_id,
        'handicap'::TEXT AS market,
        change_time,
        handicap_raw AS line_raw,
        handicap_value AS line_value,
        home_odds AS matched_odds,
        'home'::TEXT AS odds_side
    FROM public.titan007_handicap_changes
    WHERE company_id <> 4
      AND source_status <> '滚'
      AND home_odds < 0.700

    UNION ALL

    -- Handicap: away odds below 0.700.
    SELECT
        match_id,
        company_id,
        'handicap'::TEXT AS market,
        change_time,
        handicap_raw AS line_raw,
        handicap_value AS line_value,
        away_odds AS matched_odds,
        'away'::TEXT AS odds_side
    FROM public.titan007_handicap_changes
    WHERE company_id <> 4
      AND source_status <> '滚'
      AND away_odds < 0.700

    UNION ALL

    -- Over/under: over odds below 0.700.
    SELECT
        match_id,
        company_id,
        'over_under'::TEXT AS market,
        change_time,
        total_line_raw AS line_raw,
        total_line_value AS line_value,
        over_odds AS matched_odds,
        'over'::TEXT AS odds_side
    FROM public.titan007_over_under_changes
    WHERE company_id <> 4
      AND source_status <> '滚'
      AND over_odds < 0.700
)
SELECT
    hits.match_id,
    hits.company_id,
    hits.market,
    hits.change_time,
    hits.line_raw,
    hits.line_value,
    hits.matched_odds,
    hits.odds_side
FROM filter_hits AS hits
INNER JOIN company_three_matches AS company_three
    ON company_three.match_id = hits.match_id;


-- Dropping only the derived summary permits removing/reordering old columns.
DROP VIEW IF EXISTS public.match_odds_filter_summary;

CREATE VIEW public.match_odds_filter_summary AS
SELECT
    match_id,
    COUNT(DISTINCT company_id) FILTER (
        WHERE market = 'over_under'
          AND odds_side = 'over'
    ) AS over_under_company_count,
    COUNT(DISTINCT company_id) FILTER (
        WHERE market = 'handicap'
          AND odds_side = 'home'
    ) AS handicap_home_company_count,
    COUNT(DISTINCT company_id) FILTER (
        WHERE market = 'handicap'
          AND odds_side = 'away'
    ) AS handicap_away_company_count
FROM public.match_odds_filter_hits
GROUP BY match_id
HAVING COUNT(DISTINCT company_id) FILTER (
           WHERE market = 'over_under'
             AND odds_side = 'over'
       ) >= 3
    OR COUNT(DISTINCT company_id) FILTER (
           WHERE market = 'handicap'
             AND odds_side = 'home'
       ) >= 3
    OR COUNT(DISTINCT company_id) FILTER (
           WHERE market = 'handicap'
             AND odds_side = 'away'
       ) >= 3;


CREATE OR REPLACE VIEW public.match_odds_filter_market_summary AS
WITH categorized_hits AS (
    SELECT
        match_id,
        company_id,
        CASE
            WHEN market = 'over_under' AND odds_side = 'over'
                THEN 'over_under'
            WHEN market = 'handicap' AND odds_side = 'home'
                THEN 'handicap_home'
            WHEN market = 'handicap' AND odds_side = 'away'
                THEN 'handicap_away'
        END AS market_type,
        line_value
    FROM public.match_odds_filter_hits
),
qualified_markets AS (
    SELECT
        match_id,
        market_type,
        COUNT(DISTINCT company_id) AS company_count,
        MAX(line_value) AS line_value
    FROM categorized_hits
    WHERE market_type IS NOT NULL
    GROUP BY match_id, market_type
    HAVING COUNT(DISTINCT company_id) >= 3
),
scored_markets AS (
    SELECT
        qualified.match_id,
        qualified.market_type,
        qualified.company_count,
        qualified.line_value,
        details.home_score,
        details.away_score,
        CASE
            WHEN details.home_score IS NULL OR details.away_score IS NULL
                THEN NULL
            ELSE details.home_score + details.away_score
        END AS total_goals,
        CASE
            WHEN details.home_score IS NULL
              OR details.away_score IS NULL
              OR qualified.line_value IS NULL
                THEN NULL
            WHEN qualified.market_type = 'over_under'
                THEN details.home_score
                   + details.away_score
                   - qualified.line_value
            WHEN qualified.market_type = 'handicap_home'
                THEN details.home_score
                   - details.away_score
                   - qualified.line_value
            WHEN qualified.market_type = 'handicap_away'
                THEN details.away_score
                   - details.home_score
                   + qualified.line_value
        END AS settlement_margin
    FROM qualified_markets AS qualified
    LEFT JOIN public.match_details AS details
        ON details.match_id = qualified.match_id
)
SELECT
    match_id,
    market_type,
    company_count,
    line_value,
    home_score,
    away_score,
    total_goals,
    CASE
        WHEN settlement_margin IS NULL THEN NULL
        WHEN settlement_margin >= 0.5 THEN '全赢'
        WHEN settlement_margin > 0 THEN '赢半'
        WHEN settlement_margin = 0 THEN '走水'
        WHEN settlement_margin > -0.5 THEN '输半'
        ELSE '全输'
    END AS result
FROM scored_markets;


-- Verify that all three views now exist. All columns should be non-NULL.
SELECT
    to_regclass('public.match_odds_filter_hits') AS hits_view,
    to_regclass('public.match_odds_filter_summary') AS summary_view,
    to_regclass(
        'public.match_odds_filter_market_summary'
    ) AS market_summary_view;
