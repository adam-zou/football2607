"""Shared persistence rules for completing match crawl work."""

from typing import Any, Sequence


REQUIRED_COMPANY_IDS = (3, 4, 8, 24, 31, 47)


MARK_MATCHES_COMPLETED = """
UPDATE match_status AS status
SET crawl_status = '已完成',
    updated_at = NOW()
FROM match_basic_info AS basic
WHERE status.match_id = basic.match_id
  AND status.crawl_status = '未完成'
  AND status.match_id = ANY(%s::BIGINT[])
  AND basic.status_text = '完'
  AND CASE
      WHEN basic.scheduled_time ~ '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
      THEN basic.scheduled_time::TIMESTAMP
          <= (NOW() AT TIME ZONE 'Asia/Shanghai') - INTERVAL '3 hours'
      ELSE FALSE
  END
  AND (
      SELECT COUNT(*)
      FROM titan007_odds_fetch_status AS odds_status
      WHERE odds_status.match_id = status.match_id
        AND odds_status.company_id IN (3, 4, 8, 24, 31, 47)
        AND odds_status.handicap_completed
        AND odds_status.one_x_two_completed
        AND odds_status.over_under_completed
        AND odds_status.verification_version = 1
  ) = 6
"""


def mark_matches_completed(cursor: Any, match_ids: Sequence[int]) -> None:
    """Mark only matches that currently satisfy every completion condition."""

    normalized_ids = [int(match_id) for match_id in match_ids]
    if not normalized_ids:
        return

    # fetch-odds 也允许处理尚未进入比赛同步库的 ID。若两张比赛表还没有
    # 初始化，赔率仍可正常落库，只是暂时没有 crawl_status 可更新。
    cursor.execute(
        "SELECT to_regclass('match_status'), to_regclass('match_basic_info')"
    )
    relations = cursor.fetchone()
    if relations and all(relations):
        cursor.execute(MARK_MATCHES_COMPLETED, (normalized_ids,))
