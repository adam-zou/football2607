"""Read-only daily collection statistics for the SimpleCrawler dashboard."""

from __future__ import annotations

from datetime import date
from typing import Dict, List

import psycopg2
from psycopg2.extensions import connection as Connection

from .companies import COMPANY_NAMES


DailyStatistics = Dict[str, object]

TODAYS_MATCHES_CTE = """
WITH todays_matches AS (
    SELECT details.match_id, details.status_text, ids.crawl_status
    FROM match_details AS details
    JOIN match_ids AS ids USING (match_id)
    WHERE details.scheduled_time ~
        '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
      AND details.scheduled_time::TIMESTAMP::DATE =
          (NOW() AT TIME ZONE 'Asia/Shanghai')::DATE
)
"""

MATCH_SUMMARY_SQL = TODAYS_MATCHES_CTE + """
SELECT
    (NOW() AT TIME ZONE 'Asia/Shanghai')::DATE,
    COUNT(*),
    COUNT(*) FILTER (WHERE status_text = '未开始'),
    COUNT(*) FILTER (
        WHERE status_text IN ('上', '中', '下', '加', '点', '进行中')
           OR status_text ~ '^[0-9]+(\\+[0-9]+)?(''|′)$'
    ),
    COUNT(*) FILTER (WHERE status_text = '完'),
    COUNT(*) FILTER (WHERE status_text = '推迟'),
    COUNT(*) FILTER (WHERE status_text = '取消'),
    COUNT(*) FILTER (WHERE status_text = '待定'),
    COUNT(*) FILTER (
        WHERE status_text NOT IN (
            '未开始', '上', '中', '下', '加', '点', '进行中',
            '完', '推迟', '取消', '待定'
        )
          AND status_text !~ '^[0-9]+(\\+[0-9]+)?(''|′)$'
    ),
    COUNT(*) FILTER (WHERE crawl_status = '未完成'),
    COUNT(*) FILTER (WHERE crawl_status = '已完成'),
    COUNT(*) FILTER (WHERE crawl_status = '暂停爬取'),
    COUNT(*) FILTER (WHERE crawl_status = '异常'),
    COUNT(*) FILTER (
        WHERE status_text = '完' AND crawl_status = '未完成'
    )
FROM todays_matches
"""

HISTORICAL_MATCH_SUMMARY_SQL = """
SELECT
    COUNT(*),
    COUNT(*) FILTER (WHERE details.status_text = '未开始'),
    COUNT(*) FILTER (
        WHERE details.status_text IN ('上', '中', '下', '加', '点', '进行中')
           OR details.status_text ~ '^[0-9]+(\\+[0-9]+)?(''|′)$'
    ),
    COUNT(*) FILTER (WHERE details.status_text = '完'),
    COUNT(*) FILTER (WHERE details.status_text = '推迟'),
    COUNT(*) FILTER (WHERE details.status_text = '取消'),
    COUNT(*) FILTER (WHERE details.status_text = '待定'),
    COUNT(*) FILTER (
        WHERE details.status_text NOT IN (
            '未开始', '上', '中', '下', '加', '点', '进行中',
            '完', '推迟', '取消', '待定'
        )
          AND details.status_text !~
              '^[0-9]+(\\+[0-9]+)?(''|′)$'
    ),
    COUNT(*) FILTER (WHERE ids.crawl_status = '未完成'),
    COUNT(*) FILTER (WHERE ids.crawl_status = '已完成'),
    COUNT(*) FILTER (WHERE ids.crawl_status = '暂停爬取'),
    COUNT(*) FILTER (WHERE ids.crawl_status = '异常'),
    COUNT(*) FILTER (
        WHERE details.status_text = '完'
          AND ids.crawl_status = '未完成'
    )
FROM match_details AS details
JOIN match_ids AS ids USING (match_id)
WHERE details.scheduled_time ~
    '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
  AND details.scheduled_time::TIMESTAMP::DATE <
      (NOW() AT TIME ZONE 'Asia/Shanghai')::DATE
"""

DATA_QUALITY_SQL = """
SELECT
    COUNT(*) FILTER (WHERE details.match_id IS NULL),
    COUNT(*) FILTER (
        WHERE details.match_id IS NOT NULL
          AND details.scheduled_time !~
              '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
    )
FROM match_ids AS ids
LEFT JOIN match_details AS details USING (match_id)
"""

ODDS_COUNTS_SQL = TODAYS_MATCHES_CTE + """
SELECT company_id, market, row_count
FROM (
    SELECT changes.company_id, 'handicap' AS market, COUNT(*) AS row_count
    FROM titan007_handicap_changes AS changes
    JOIN todays_matches USING (match_id)
    GROUP BY changes.company_id

    UNION ALL

    SELECT changes.company_id, 'one_x_two' AS market, COUNT(*) AS row_count
    FROM titan007_1x2_changes AS changes
    JOIN todays_matches USING (match_id)
    GROUP BY changes.company_id

    UNION ALL

    SELECT changes.company_id, 'over_under' AS market, COUNT(*) AS row_count
    FROM titan007_over_under_changes AS changes
    JOIN todays_matches USING (match_id)
    GROUP BY changes.company_id
) AS counts
ORDER BY company_id, market
"""


def empty_odds_counts() -> List[Dict[str, object]]:
    return [
        {
            "company_id": company_id,
            "company_name": company_name,
            "handicap": 0,
            "one_x_two": 0,
            "over_under": 0,
        }
        for company_id, company_name in COMPANY_NAMES.items()
    ]


def collect_daily_statistics(connection: Connection) -> DailyStatistics:
    """Collect today's match-state and odds-record counts in Shanghai time."""

    with connection.cursor() as cursor:
        cursor.execute(MATCH_SUMMARY_SQL)
        (
            day,
            match_count,
            not_started_count,
            in_progress_count,
            finished_count,
            postponed_count,
            cancelled_count,
            pending_count,
            other_status_count,
            crawl_unfinished_count,
            crawl_completed_count,
            paused_count,
            abnormal_count,
            finished_unfinished_count,
        ) = cursor.fetchone()
        cursor.execute(HISTORICAL_MATCH_SUMMARY_SQL)
        (
            historical_match_count,
            historical_not_started_count,
            historical_in_progress_count,
            historical_finished_count,
            historical_postponed_count,
            historical_cancelled_count,
            historical_pending_count,
            historical_other_status_count,
            historical_unfinished_count,
            historical_completed_count,
            historical_paused_count,
            historical_abnormal_count,
            historical_finished_unfinished_count,
        ) = cursor.fetchone()
        cursor.execute(DATA_QUALITY_SQL)
        missing_details_count, invalid_scheduled_time_count = cursor.fetchone()
        cursor.execute(ODDS_COUNTS_SQL)
        stored_counts = {
            (int(company_id), str(market)): int(row_count)
            for company_id, market, row_count in cursor.fetchall()
        }

    odds_counts = empty_odds_counts()
    for company in odds_counts:
        company_id = int(company["company_id"])
        for market in ("handicap", "one_x_two", "over_under"):
            company[market] = stored_counts.get((company_id, market), 0)

    return {
        "date": day.isoformat() if isinstance(day, date) else str(day),
        "match_count": int(match_count),
        "not_started_count": int(not_started_count),
        "finished_count": int(finished_count),
        "in_progress_count": int(in_progress_count),
        "postponed_count": int(postponed_count),
        "cancelled_count": int(cancelled_count),
        "pending_count": int(pending_count),
        "other_status_count": int(other_status_count),
        "crawl_unfinished_count": int(crawl_unfinished_count),
        "crawl_completed_count": int(crawl_completed_count),
        "abnormal_count": int(abnormal_count),
        "paused_count": int(paused_count),
        "finished_unfinished_count": int(finished_unfinished_count),
        "historical_match_count": int(historical_match_count),
        "historical_not_started_count": int(historical_not_started_count),
        "historical_in_progress_count": int(historical_in_progress_count),
        "historical_finished_count": int(historical_finished_count),
        "historical_postponed_count": int(historical_postponed_count),
        "historical_cancelled_count": int(historical_cancelled_count),
        "historical_pending_count": int(historical_pending_count),
        "historical_other_status_count": int(historical_other_status_count),
        "historical_unfinished_count": int(historical_unfinished_count),
        "historical_completed_count": int(historical_completed_count),
        "historical_paused_count": int(historical_paused_count),
        "historical_abnormal_count": int(historical_abnormal_count),
        "historical_finished_unfinished_count": int(
            historical_finished_unfinished_count
        ),
        "missing_details_count": int(missing_details_count),
        "invalid_scheduled_time_count": int(invalid_scheduled_time_count),
        "odds_counts": odds_counts,
    }


def fetch_daily_statistics(database_url: str) -> DailyStatistics:
    """Open a short-lived read-only database session for one snapshot."""

    if not database_url:
        raise ValueError("SIMPLE_CRAWLER_DATABASE_URL 未配置")
    with psycopg2.connect(database_url) as connection:
        connection.set_session(readonly=True)
        return collect_daily_statistics(connection)
