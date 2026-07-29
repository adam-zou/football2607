-- PostgreSQL
--
-- Build a session-scoped snapshot of PB warning matches.
--
-- Rules:
--   * Read company 47 live one-x-two suspension rows.
--   * The suspension run must start from match minute 0 through 70, except 45.
--   * Prove at least three elapsed minutes with the next one-x-two row, the
--     run's own consecutive rows, or (for an open run) the first company 47
--     over-under row at least three minutes after the suspension started.
--   * Keep the first qualifying suspension run per match.
--   * Read the warning line only from a company 47 over-under row recorded in
--     the exact warning minute (suspension start plus three minutes).
--   * Keep only matches whose final over-under row in that minute has a
--     numeric total line of exactly 1.5.
--   * Do not filter by match date or match status.
--
-- The table exists only for the current database session. Run this file with
-- \i inside an existing psql session if subsequent statements need the table.

DROP TABLE IF EXISTS pg_temp.pb_warning_matches_temp;

CREATE TEMPORARY TABLE pb_warning_matches_temp
ON COMMIT PRESERVE ROWS
AS
WITH suspended_1x2_raw AS (
    SELECT
        details.match_id,
        changes.seq,
        changes.match_minute,
        details.scheduled_time::TIMESTAMP AS scheduled_at,
        TO_TIMESTAMP(
            EXTRACT(YEAR FROM details.scheduled_time::TIMESTAMP)::INTEGER
            || '-' || changes.change_time,
            'YYYY-MM-DD HH24:MI'
        ) AS raw_change_at
    FROM public.titan007_1x2_changes AS changes
    JOIN public.match_details AS details
      ON details.match_id = changes.match_id
    WHERE changes.company_id = 47
      AND changes.source_status = '滚'
      AND changes.is_suspended = TRUE
      AND details.scheduled_time
              ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}$'
      AND changes.change_time
              ~ '^[0-9]{1,2}-[0-9]{1,2} [0-9]{1,2}:[0-9]{2}$'
),
suspended_1x2 AS (
    SELECT
        suspended_1x2_raw.*,
        CASE
            WHEN raw_change_at < scheduled_at - INTERVAL '180 days'
                THEN raw_change_at + INTERVAL '1 year'
            WHEN raw_change_at > scheduled_at + INTERVAL '180 days'
                THEN raw_change_at - INTERVAL '1 year'
            ELSE raw_change_at
        END AS change_at
    FROM suspended_1x2_raw
),
suspended_1x2_grouped AS (
    SELECT
        suspended_1x2.*,
        seq - ROW_NUMBER() OVER (
            PARTITION BY match_id
            ORDER BY seq
        ) AS suspension_group
    FROM suspended_1x2
),
suspension_runs AS (
    SELECT
        match_id,
        suspension_group,
        MIN(seq) AS start_seq,
        MAX(seq) AS end_seq,
        (ARRAY_AGG(scheduled_at ORDER BY seq))[1] AS scheduled_at,
        (ARRAY_AGG(change_at ORDER BY seq))[1] AS start_at,
        (ARRAY_AGG(change_at ORDER BY seq DESC))[1] AS last_suspended_at,
        (ARRAY_AGG(match_minute ORDER BY seq))[1] AS start_match_minute
    FROM suspended_1x2_grouped
    GROUP BY match_id, suspension_group
),
suspension_runs_with_evidence AS (
    SELECT
        runs.*,
        CASE
            WHEN next_1x2.change_at IS NOT NULL
                THEN next_1x2.change_at
            WHEN runs.last_suspended_at
                    >= runs.start_at + INTERVAL '3 minutes'
                THEN runs.last_suspended_at
            ELSE totals_evidence.change_at
        END AS qualification_evidence_at,
        CASE
            WHEN next_1x2.change_at IS NOT NULL
                THEN 'next_1x2_row'
            WHEN runs.last_suspended_at
                    >= runs.start_at + INTERVAL '3 minutes'
                THEN 'consecutive_suspension_rows'
            WHEN totals_evidence.change_at IS NOT NULL
                THEN 'over_under_heartbeat'
            ELSE NULL
        END AS qualification_evidence_type
    FROM suspension_runs AS runs

    -- The primary key supports this exact match/company/seq lookup.
    LEFT JOIN LATERAL (
        SELECT normalized.change_at
        FROM (
            SELECT
                CASE
                    WHEN parsed.raw_change_at
                            < runs.scheduled_at - INTERVAL '180 days'
                        THEN parsed.raw_change_at + INTERVAL '1 year'
                    WHEN parsed.raw_change_at
                            > runs.scheduled_at + INTERVAL '180 days'
                        THEN parsed.raw_change_at - INTERVAL '1 year'
                    ELSE parsed.raw_change_at
                END AS change_at
            FROM public.titan007_1x2_changes AS changes
            CROSS JOIN LATERAL (
                SELECT TO_TIMESTAMP(
                    EXTRACT(YEAR FROM runs.scheduled_at)::INTEGER
                    || '-' || changes.change_time,
                    'YYYY-MM-DD HH24:MI'
                ) AS raw_change_at
            ) AS parsed
            WHERE changes.match_id = runs.match_id
              AND changes.company_id = 47
              AND changes.seq = runs.end_seq + 1
              AND changes.change_time
                      ~ '^[0-9]{1,2}-[0-9]{1,2} [0-9]{1,2}:[0-9]{2}$'
        ) AS normalized
    ) AS next_1x2 ON TRUE

    -- Only open runs need an over-under heartbeat fallback.
    LEFT JOIN LATERAL (
        SELECT normalized.change_at
        FROM (
            SELECT
                totals.seq,
                CASE
                    WHEN parsed.raw_change_at
                            < runs.scheduled_at - INTERVAL '180 days'
                        THEN parsed.raw_change_at + INTERVAL '1 year'
                    WHEN parsed.raw_change_at
                            > runs.scheduled_at + INTERVAL '180 days'
                        THEN parsed.raw_change_at - INTERVAL '1 year'
                    ELSE parsed.raw_change_at
                END AS change_at
            FROM public.titan007_over_under_changes AS totals
            CROSS JOIN LATERAL (
                SELECT TO_TIMESTAMP(
                    EXTRACT(YEAR FROM runs.scheduled_at)::INTEGER
                    || '-' || totals.change_time,
                    'YYYY-MM-DD HH24:MI'
                ) AS raw_change_at
            ) AS parsed
            WHERE totals.match_id = runs.match_id
              AND totals.company_id = 47
              AND totals.change_time
                      ~ '^[0-9]{1,2}-[0-9]{1,2} [0-9]{1,2}:[0-9]{2}$'
        ) AS normalized
        WHERE normalized.change_at >= runs.start_at + INTERVAL '3 minutes'
        ORDER BY normalized.change_at ASC, normalized.seq ASC
        LIMIT 1
    ) AS totals_evidence
      ON next_1x2.change_at IS NULL
     AND runs.last_suspended_at < runs.start_at + INTERVAL '3 minutes'
),
qualifying_runs AS (
    SELECT
        evidence.*,
        evidence.start_at + INTERVAL '3 minutes' AS warning_at,
        EXTRACT(
            EPOCH FROM (
                evidence.qualification_evidence_at - evidence.start_at
            )
        ) / 60 AS proven_duration_minutes
    FROM suspension_runs_with_evidence AS evidence
    WHERE evidence.start_match_minute BETWEEN 0 AND 70
      AND evidence.start_match_minute <> 45
      AND evidence.qualification_evidence_at IS NOT NULL
      AND evidence.qualification_evidence_at
              >= evidence.start_at + INTERVAL '3 minutes'
),
first_warning_per_match AS (
    SELECT DISTINCT ON (match_id)
        match_id,
        start_seq,
        start_match_minute,
        scheduled_at,
        start_at AS suspension_start_at,
        warning_at,
        qualification_evidence_at,
        qualification_evidence_type,
        proven_duration_minutes
    FROM qualifying_runs
    ORDER BY match_id, start_seq
)
SELECT
    details.match_id,
    details.league,
    details.scheduled_time,
    details.status_text,
    details.home_team,
    details.home_score,
    details.away_score,
    details.away_team,
    warning.start_match_minute,
    warning.suspension_start_at,
    warning.warning_at,
    warning.qualification_evidence_at,
    warning.qualification_evidence_type,
    warning.proven_duration_minutes,
    warning_totals.total_line_raw AS warning_line,
    warning_totals.change_at AS warning_line_time
FROM first_warning_per_match AS warning
JOIN public.match_details AS details
  ON details.match_id = warning.match_id
LEFT JOIN LATERAL (
    SELECT
        normalized.total_line_raw,
        normalized.total_line_value,
        normalized.change_at
    FROM (
        SELECT
            totals.seq,
            totals.total_line_raw,
            totals.total_line_value,
            CASE
                WHEN parsed.raw_change_at
                        < warning.scheduled_at - INTERVAL '180 days'
                    THEN parsed.raw_change_at + INTERVAL '1 year'
                WHEN parsed.raw_change_at
                        > warning.scheduled_at + INTERVAL '180 days'
                    THEN parsed.raw_change_at - INTERVAL '1 year'
                ELSE parsed.raw_change_at
            END AS change_at
        FROM public.titan007_over_under_changes AS totals
        CROSS JOIN LATERAL (
            SELECT TO_TIMESTAMP(
                EXTRACT(YEAR FROM warning.scheduled_at)::INTEGER
                || '-' || totals.change_time,
                'YYYY-MM-DD HH24:MI'
            ) AS raw_change_at
        ) AS parsed
        WHERE totals.match_id = warning.match_id
          AND totals.company_id = 47
          AND totals.change_time
                  ~ '^[0-9]{1,2}-[0-9]{1,2} [0-9]{1,2}:[0-9]{2}$'
    ) AS normalized
    WHERE DATE_TRUNC('minute', normalized.change_at)
              = DATE_TRUNC('minute', warning.warning_at)
    ORDER BY normalized.seq DESC
    LIMIT 1
) AS warning_totals ON TRUE
WHERE warning_totals.total_line_value = 1.5
ORDER BY details.scheduled_time ASC, details.match_id ASC;

SELECT *
FROM pb_warning_matches_temp
ORDER BY scheduled_time ASC, match_id ASC;

-- Goal distribution for the filtered PB warning matches. Percentages use all
-- rows with both score values present as the denominator. Zero-goal matches
-- are reported separately, so the three requested percentages may total less
-- than 100 percent.
WITH goal_totals AS (
    SELECT
        home_score + away_score AS total_goals
    FROM pb_warning_matches_temp
    WHERE home_score IS NOT NULL
      AND away_score IS NOT NULL
)
SELECT
    (SELECT COUNT(*) FROM pb_warning_matches_temp) AS total_matches,
    COUNT(*) AS scored_matches,
    COUNT(*) FILTER (WHERE total_goals = 0) AS zero_goal_matches,
    COUNT(*) FILTER (WHERE total_goals = 1) AS one_goal_matches,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE total_goals = 1)
        / NULLIF(COUNT(*), 0),
        2
    ) AS one_goal_percentage,
    COUNT(*) FILTER (WHERE total_goals = 2) AS two_goal_matches,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE total_goals = 2)
        / NULLIF(COUNT(*), 0),
        2
    ) AS two_goal_percentage,
    COUNT(*) FILTER (WHERE total_goals >= 3) AS three_plus_goal_matches,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE total_goals >= 3)
        / NULLIF(COUNT(*), 0),
        2
    ) AS three_plus_goal_percentage,
    (SELECT COUNT(*) FROM pb_warning_matches_temp)
        - COUNT(*) AS missing_score_matches
FROM goal_totals;
