"""PostgreSQL persistence for Titan007 odds-change snapshots."""

import asyncio
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extensions import connection as Connection
from psycopg2.extras import execute_values

from .match_completion import mark_matches_completed
from .models import (
    HandicapChange,
    Movement,
    OddsChange,
    OddsMarketRequest,
    OddsSnapshot,
    OneXTwoChange,
    OverUnderChange,
)
from .schema import load_migrations


INITIALIZE_ODDS_TABLES = load_migrations(
    (
        "003_titan007_odds_changes.sql",
        "004_odds_schedule.sql",
    )
)


UPSERT_HANDICAP = """
INSERT INTO titan007_handicap_changes (
    match_id, company_id, seq, match_minute, home_score, away_score,
    change_time, source_status, is_suspended,
    home_odds, home_odds_movement, handicap_raw, handicap_value,
    handicap_movement, away_odds, away_odds_movement
)
VALUES %s
ON CONFLICT (match_id, company_id, seq) DO UPDATE SET
    match_minute = EXCLUDED.match_minute,
    home_score = EXCLUDED.home_score,
    away_score = EXCLUDED.away_score,
    change_time = EXCLUDED.change_time,
    source_status = EXCLUDED.source_status,
    is_suspended = EXCLUDED.is_suspended,
    home_odds = EXCLUDED.home_odds,
    home_odds_movement = EXCLUDED.home_odds_movement,
    handicap_raw = EXCLUDED.handicap_raw,
    handicap_value = EXCLUDED.handicap_value,
    handicap_movement = EXCLUDED.handicap_movement,
    away_odds = EXCLUDED.away_odds,
    away_odds_movement = EXCLUDED.away_odds_movement,
    updated_at = NOW()
"""


UPSERT_ONE_X_TWO = """
INSERT INTO titan007_1x2_changes (
    match_id, company_id, seq, match_minute, home_score, away_score,
    change_time, source_status, is_suspended,
    home_win_odds, home_win_odds_movement, draw_odds, draw_odds_movement,
    away_win_odds, away_win_odds_movement
)
VALUES %s
ON CONFLICT (match_id, company_id, seq) DO UPDATE SET
    match_minute = EXCLUDED.match_minute,
    home_score = EXCLUDED.home_score,
    away_score = EXCLUDED.away_score,
    change_time = EXCLUDED.change_time,
    source_status = EXCLUDED.source_status,
    is_suspended = EXCLUDED.is_suspended,
    home_win_odds = EXCLUDED.home_win_odds,
    home_win_odds_movement = EXCLUDED.home_win_odds_movement,
    draw_odds = EXCLUDED.draw_odds,
    draw_odds_movement = EXCLUDED.draw_odds_movement,
    away_win_odds = EXCLUDED.away_win_odds,
    away_win_odds_movement = EXCLUDED.away_win_odds_movement,
    updated_at = NOW()
"""


UPSERT_OVER_UNDER = """
INSERT INTO titan007_over_under_changes (
    match_id, company_id, seq, match_minute, home_score, away_score,
    change_time, source_status, is_suspended,
    over_odds, over_odds_movement, total_line_raw, total_line_value,
    total_line_movement, under_odds, under_odds_movement
)
VALUES %s
ON CONFLICT (match_id, company_id, seq) DO UPDATE SET
    match_minute = EXCLUDED.match_minute,
    home_score = EXCLUDED.home_score,
    away_score = EXCLUDED.away_score,
    change_time = EXCLUDED.change_time,
    source_status = EXCLUDED.source_status,
    is_suspended = EXCLUDED.is_suspended,
    over_odds = EXCLUDED.over_odds,
    over_odds_movement = EXCLUDED.over_odds_movement,
    total_line_raw = EXCLUDED.total_line_raw,
    total_line_value = EXCLUDED.total_line_value,
    total_line_movement = EXCLUDED.total_line_movement,
    under_odds = EXCLUDED.under_odds,
    under_odds_movement = EXCLUDED.under_odds_movement,
    updated_at = NOW()
"""


UPSERT_HANDICAP_FETCH_STATUS = """
INSERT INTO titan007_odds_fetch_status (
    match_id, company_id, handicap_completed, handicap_last_seq,
    verification_version
)
VALUES %s
ON CONFLICT (match_id, company_id) DO UPDATE SET
    handicap_completed = EXCLUDED.handicap_completed,
    handicap_last_seq = EXCLUDED.handicap_last_seq,
    verification_version = EXCLUDED.verification_version,
    final_verified_at = NULL,
    updated_at = NOW()
"""


UPSERT_ONE_X_TWO_FETCH_STATUS = UPSERT_HANDICAP_FETCH_STATUS.replace(
    "handicap_completed", "one_x_two_completed"
).replace("handicap_last_seq", "one_x_two_last_seq")


UPSERT_OVER_UNDER_FETCH_STATUS = UPSERT_HANDICAP_FETCH_STATUS.replace(
    "handicap_completed", "over_under_completed"
).replace("handicap_last_seq", "over_under_last_seq")


FINALIZE_ODDS_FETCH_STATUS = """
UPDATE titan007_odds_fetch_status
SET final_verified_at = CASE
        WHEN handicap_completed
         AND one_x_two_completed
         AND over_under_completed
         AND verification_version = 1
        THEN COALESCE(final_verified_at, NOW())
        ELSE NULL
    END,
    updated_at = NOW()
WHERE match_id = %s
"""


COMMON_VERIFY_COLUMNS = (
    "match_minute",
    "home_score",
    "away_score",
    "change_time",
    "source_status",
    "is_suspended",
)


def _verification_statement(table: str, market_columns: Sequence[str]) -> str:
    columns = COMMON_VERIFY_COLUMNS + tuple(market_columns)
    row_columns = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return f"""
SELECT ROW({row_columns}) IS NOT DISTINCT FROM ROW({placeholders})
FROM {table}
WHERE match_id = %s AND company_id = %s AND seq = %s
  AND seq = (
      SELECT MAX(latest.seq)
      FROM {table} AS latest
      WHERE latest.match_id = %s AND latest.company_id = %s
  )
"""


def _empty_market_statement(table: str) -> str:
    return f"""
SELECT NOT EXISTS (
    SELECT 1 FROM {table} WHERE match_id = %s AND company_id = %s
)
"""


VERIFY_HANDICAP = _verification_statement(
    "titan007_handicap_changes",
    (
        "home_odds",
        "home_odds_movement",
        "handicap_raw",
        "handicap_value",
        "handicap_movement",
        "away_odds",
        "away_odds_movement",
    ),
)
VERIFY_ONE_X_TWO = _verification_statement(
    "titan007_1x2_changes",
    (
        "home_win_odds",
        "home_win_odds_movement",
        "draw_odds",
        "draw_odds_movement",
        "away_win_odds",
        "away_win_odds_movement",
    ),
)
VERIFY_OVER_UNDER = _verification_statement(
    "titan007_over_under_changes",
    (
        "over_odds",
        "over_odds_movement",
        "total_line_raw",
        "total_line_value",
        "total_line_movement",
        "under_odds",
        "under_odds_movement",
    ),
)
VERIFY_EMPTY_HANDICAP = _empty_market_statement("titan007_handicap_changes")
VERIFY_EMPTY_ONE_X_TWO = _empty_market_statement("titan007_1x2_changes")
VERIFY_EMPTY_OVER_UNDER = _empty_market_statement(
    "titan007_over_under_changes"
)


FETCH_PENDING_MATCH_IDS = """
SELECT status.match_id
FROM match_status AS status
JOIN match_basic_info AS basic ON basic.match_id = status.match_id
LEFT JOIN titan007_odds_fetch_status AS odds_status
  ON odds_status.match_id = status.match_id
WHERE status.crawl_status = '未完成'
  AND EXISTS (
      SELECT 1
      FROM (VALUES (3), (4), (8), (24), (31), (47)) AS company(company_id)
      CROSS JOIN (
          VALUES ('handicap'), ('one_x_two'), ('over_under')
      ) AS market(market)
      LEFT JOIN titan007_odds_market_schedule AS schedule
        ON schedule.match_id = status.match_id
       AND schedule.company_id = company.company_id
       AND schedule.market = market.market
      WHERE COALESCE(
          schedule.next_attempt_at,
          '-infinity'::TIMESTAMPTZ
      ) <= NOW()
  )
  AND (
      (
          basic.status_text <> '完'
          AND basic.scheduled_at <= NOW() + INTERVAL '24 hours'
      )
      OR (
          basic.status_text = '完'
          AND basic.scheduled_at <= NOW() - INTERVAL '3 hours'
      )
  )
GROUP BY status.match_id, basic.status_text, basic.scheduled_at
HAVING COUNT(*) FILTER (
    WHERE odds_status.company_id IN (3, 4, 8, 24, 31, 47)
      AND odds_status.handicap_completed
      AND odds_status.one_x_two_completed
      AND odds_status.over_under_completed
      AND odds_status.verification_version = 1
) < 6
ORDER BY CASE
             WHEN basic.status_text <> '完'
              AND basic.scheduled_at <= NOW() + INTERVAL '5 minutes'
             THEN 0
             WHEN basic.status_text = '完' THEN 1
             ELSE 2
         END,
         (
             SELECT MIN(schedule.next_attempt_at)
             FROM titan007_odds_market_schedule AS schedule
             WHERE schedule.match_id = status.match_id
         ) ASC NULLS FIRST,
         status.match_id
LIMIT %s
"""


COUNT_PENDING_MATCH_IDS = """
SELECT COUNT(*)
FROM (
    SELECT status.match_id
    FROM match_status AS status
    JOIN match_basic_info AS basic ON basic.match_id = status.match_id
    LEFT JOIN titan007_odds_fetch_status AS odds_status
      ON odds_status.match_id = status.match_id
    WHERE status.crawl_status = '未完成'
      AND EXISTS (
          SELECT 1
          FROM (VALUES (3), (4), (8), (24), (31), (47)) AS company(company_id)
          CROSS JOIN (
              VALUES ('handicap'), ('one_x_two'), ('over_under')
          ) AS market(market)
          LEFT JOIN titan007_odds_market_schedule AS schedule
            ON schedule.match_id = status.match_id
           AND schedule.company_id = company.company_id
           AND schedule.market = market.market
          WHERE COALESCE(
              schedule.next_attempt_at,
              '-infinity'::TIMESTAMPTZ
          ) <= NOW()
      )
      AND (
          (
              basic.status_text <> '完'
              AND basic.scheduled_at <= NOW() + INTERVAL '24 hours'
          )
          OR (
              basic.status_text = '完'
              AND basic.scheduled_at <= NOW() - INTERVAL '3 hours'
          )
      )
    GROUP BY status.match_id
    HAVING COUNT(*) FILTER (
        WHERE odds_status.company_id IN (3, 4, 8, 24, 31, 47)
          AND odds_status.handicap_completed
          AND odds_status.one_x_two_completed
          AND odds_status.over_under_completed
          AND odds_status.verification_version = 1
    ) < 6
) AS pending
"""


FETCH_DUE_MARKETS = """
SELECT company.company_id, market.market
FROM (VALUES (3), (4), (8), (24), (31), (47)) AS company(company_id)
CROSS JOIN (
    VALUES ('handicap'), ('one_x_two'), ('over_under')
) AS market(market)
LEFT JOIN titan007_odds_market_schedule AS schedule
  ON schedule.match_id = %s
 AND schedule.company_id = company.company_id
 AND schedule.market = market.market
WHERE COALESCE(schedule.next_attempt_at, '-infinity'::TIMESTAMPTZ) <= NOW()
ORDER BY company.company_id, market.market
"""


BEGIN_ODDS_ATTEMPT = """
INSERT INTO titan007_odds_market_schedule (
    match_id, company_id, market, next_attempt_at, last_attempt_at
)
VALUES %s
ON CONFLICT (match_id, company_id, market) DO UPDATE SET
    next_attempt_at = NOW() + INTERVAL '5 minutes',
    last_attempt_at = NOW(),
    updated_at = NOW()
"""


RECORD_ODDS_SUCCESS = """
INSERT INTO titan007_odds_market_schedule (
    match_id, company_id, market, consecutive_failures, next_attempt_at,
    last_attempt_at, last_succeeded_at, last_error,
    is_abnormal, abnormal_since
)
SELECT work.match_id,
       work.company_id,
       work.market,
       0,
       CASE
           WHEN basic.status_text = '完'
           THEN NOW() + INTERVAL '1 minute'
           WHEN basic.scheduled_at <= NOW() + INTERVAL '5 minutes'
           THEN NOW() + INTERVAL '1 minute'
           ELSE LEAST(
               NOW() + INTERVAL '8 hours',
               basic.scheduled_at - INTERVAL '5 minutes'
           )
       END,
       NOW(),
       NOW(),
       NULL,
       FALSE,
       NULL
FROM match_basic_info AS basic
JOIN (VALUES %s) AS work(match_id, company_id, market)
  ON work.match_id = basic.match_id
ON CONFLICT (match_id, company_id, market) DO UPDATE SET
    consecutive_failures = 0,
    next_attempt_at = EXCLUDED.next_attempt_at,
    last_attempt_at = EXCLUDED.last_attempt_at,
    last_succeeded_at = EXCLUDED.last_succeeded_at,
    last_error = NULL,
    is_abnormal = FALSE,
    abnormal_since = NULL,
    updated_at = NOW()
"""


RECORD_ODDS_FAILURE = """
INSERT INTO titan007_odds_market_schedule (
    match_id, company_id, market, consecutive_failures, next_attempt_at,
    last_attempt_at, last_error, is_abnormal, abnormal_since
)
VALUES %s
ON CONFLICT (match_id, company_id, market) DO UPDATE SET
    consecutive_failures = titan007_odds_market_schedule.consecutive_failures + 1,
    next_attempt_at = NOW() + CASE
        WHEN titan007_odds_market_schedule.consecutive_failures + 1 = 1
        THEN INTERVAL '1 minute'
        WHEN titan007_odds_market_schedule.consecutive_failures + 1 = 2
        THEN INTERVAL '2 minutes'
        WHEN titan007_odds_market_schedule.consecutive_failures + 1 = 3
        THEN INTERVAL '5 minutes'
        ELSE INTERVAL '3 hours'
    END,
    last_attempt_at = NOW(),
    last_error = EXCLUDED.last_error,
    is_abnormal = titan007_odds_market_schedule.consecutive_failures + 1 >= 4,
    abnormal_since = CASE
        WHEN titan007_odds_market_schedule.consecutive_failures + 1 >= 4
        THEN COALESCE(titan007_odds_market_schedule.abnormal_since, NOW())
        ELSE NULL
    END,
    updated_at = NOW()
"""


class PostgresOddsStore:
    """Create and transactionally upsert the three odds-change tables."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._connection: Optional[Connection] = None
        self._lock: Optional[asyncio.Lock] = None

    async def initialize(self) -> None:
        async with self._get_lock():
            await asyncio.to_thread(self._initialize_sync)

    async def upsert_snapshot(self, snapshot: OddsSnapshot) -> None:
        async with self._get_lock():
            await asyncio.to_thread(self._upsert_snapshot_sync, snapshot)

    async def fetch_pending_match_ids(self, limit: int) -> List[int]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        async with self._get_lock():
            return await asyncio.to_thread(
                self._fetch_pending_match_ids_sync,
                limit,
            )

    async def count_pending_match_ids(self) -> int:
        async with self._get_lock():
            return await asyncio.to_thread(self._count_pending_match_ids_sync)

    async def begin_match_attempt(
        self,
        match_id: int,
    ) -> List[OddsMarketRequest]:
        async with self._get_lock():
            return await asyncio.to_thread(
                self._begin_match_attempt_sync,
                match_id,
            )

    async def record_market_outcomes(self, snapshot: OddsSnapshot) -> None:
        async with self._get_lock():
            await asyncio.to_thread(
                self._record_market_outcomes_sync,
                snapshot,
            )

    async def record_market_failures(
        self,
        match_id: int,
        requests: Sequence[OddsMarketRequest],
        error: str,
    ) -> None:
        async with self._get_lock():
            await asyncio.to_thread(
                self._record_market_failures_sync,
                match_id,
                requests,
                error,
            )

    async def close(self) -> None:
        async with self._get_lock():
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _initialize_sync(self) -> None:
        self._connection = psycopg2.connect(self.dsn)
        try:
            with self._connection:
                with self._connection.cursor() as cursor:
                    cursor.execute(INITIALIZE_ODDS_TABLES)
                    cursor.execute(
                        "SELECT DISTINCT match_id "
                        "FROM titan007_odds_fetch_status"
                    )
                    mark_matches_completed(
                        cursor,
                        [int(row[0]) for row in cursor.fetchall()],
                    )
        except Exception:
            self._connection.close()
            self._connection = None
            raise

    def _upsert_snapshot_sync(self, snapshot: OddsSnapshot) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresOddsStore is not initialized")

        writes: Sequence[Tuple[str, List[Tuple[Any, ...]]]] = (
            (
                UPSERT_HANDICAP,
                [self._handicap_values(change) for change in snapshot.handicap_changes],
            ),
            (
                UPSERT_ONE_X_TWO,
                [self._one_x_two_values(change) for change in snapshot.one_x_two_changes],
            ),
            (
                UPSERT_OVER_UNDER,
                [self._over_under_values(change) for change in snapshot.over_under_changes],
            ),
        )
        with self._connection:
            with self._connection.cursor() as cursor:
                # 完成核验必须读取“上一次抓取”已经保存的数据库状态。
                # 若先 upsert 本次快照再比较，结果几乎必然一致，无法证明稳定。
                fetch_status_values = self._fetch_status_values(cursor, snapshot)
                for statement, values in writes:
                    if values:
                        execute_values(cursor, statement, values)
                fetch_status_statements = {
                    "handicap": UPSERT_HANDICAP_FETCH_STATUS,
                    "one_x_two": UPSERT_ONE_X_TWO_FETCH_STATUS,
                    "over_under": UPSERT_OVER_UNDER_FETCH_STATUS,
                }
                for market, values in fetch_status_values.items():
                    if values:
                        execute_values(
                            cursor,
                            fetch_status_statements[market],
                            values,
                        )
                if any(fetch_status_values.values()):
                    cursor.execute(
                        FINALIZE_ODDS_FETCH_STATUS,
                        (snapshot.match_id,),
                    )
                mark_matches_completed(cursor, [snapshot.match_id])

    def _fetch_pending_match_ids_sync(self, limit: int) -> List[int]:
        if self._connection is None:
            raise RuntimeError("PostgresOddsStore is not initialized")
        with self._connection.cursor() as cursor:
            cursor.execute(FETCH_PENDING_MATCH_IDS, (limit,))
            return [int(row[0]) for row in cursor.fetchall()]

    def _count_pending_match_ids_sync(self) -> int:
        if self._connection is None:
            raise RuntimeError("PostgresOddsStore is not initialized")
        with self._connection.cursor() as cursor:
            cursor.execute(COUNT_PENDING_MATCH_IDS)
            result = cursor.fetchone()
            return int(result[0]) if result else 0

    def _begin_match_attempt_sync(
        self,
        match_id: int,
    ) -> List[OddsMarketRequest]:
        if self._connection is None:
            raise RuntimeError("PostgresOddsStore is not initialized")
        with self._connection:
            with self._connection.cursor() as cursor:
                cursor.execute(FETCH_DUE_MARKETS, (int(match_id),))
                requests = [
                    OddsMarketRequest(int(row[0]), str(row[1]))
                    for row in cursor.fetchall()
                ]
                if requests:
                    execute_values(
                        cursor,
                        BEGIN_ODDS_ATTEMPT,
                        [
                            (int(match_id), request.company_id, request.market)
                            for request in requests
                        ],
                        template=(
                            "(%s, %s, %s, "
                            "NOW() + INTERVAL '5 minutes', NOW())"
                        ),
                    )
                return requests

    def _record_market_outcomes_sync(self, snapshot: OddsSnapshot) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresOddsStore is not initialized")
        with self._connection:
            with self._connection.cursor() as cursor:
                successful = [
                    (
                        int(snapshot.match_id),
                        request.company_id,
                        request.market,
                    )
                    for request in snapshot.successful_markets
                ]
                if successful:
                    execute_values(cursor, RECORD_ODDS_SUCCESS, successful)
                self._record_market_failures_with_cursor(
                    cursor,
                    snapshot.match_id,
                    snapshot.failed_markets.items(),
                )

    def _record_market_failures_sync(
        self,
        match_id: int,
        requests: Sequence[OddsMarketRequest],
        error: str,
    ) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresOddsStore is not initialized")
        with self._connection:
            with self._connection.cursor() as cursor:
                self._record_market_failures_with_cursor(
                    cursor,
                    match_id,
                    ((request, error) for request in requests),
                )

    @staticmethod
    def _record_market_failures_with_cursor(
        cursor: Any,
        match_id: int,
        failures: Iterable[Tuple[OddsMarketRequest, str]],
    ) -> None:
        values = [
            (
                int(match_id),
                request.company_id,
                request.market,
                str(error)[:1000],
            )
            for request, error in failures
        ]
        if values:
            execute_values(
                cursor,
                RECORD_ODDS_FAILURE,
                values,
                template=(
                    "(%s, %s, %s, 1, NOW() + INTERVAL '1 minute', "
                    "NOW(), %s, FALSE, NULL)"
                ),
            )

    def _fetch_status_values(
        self,
        cursor: Any,
        snapshot: OddsSnapshot,
    ) -> Dict[str, List[Tuple[Any, ...]]]:
        finalizable = self._match_is_finalizable(cursor, snapshot.match_id)
        status_values: Dict[str, List[Tuple[Any, ...]]] = {
            "handicap": [],
            "one_x_two": [],
            "over_under": [],
        }
        changes_by_market = {
            "handicap": snapshot.handicap_changes,
            "one_x_two": snapshot.one_x_two_changes,
            "over_under": snapshot.over_under_changes,
        }
        value_functions = {
            "handicap": self._handicap_values,
            "one_x_two": self._one_x_two_values,
            "over_under": self._over_under_values,
        }
        verify_statements = {
            "handicap": VERIFY_HANDICAP,
            "one_x_two": VERIFY_ONE_X_TWO,
            "over_under": VERIFY_OVER_UNDER,
        }
        empty_statements = {
            "handicap": VERIFY_EMPTY_HANDICAP,
            "one_x_two": VERIFY_EMPTY_ONE_X_TWO,
            "over_under": VERIFY_EMPTY_OVER_UNDER,
        }
        for request in snapshot.successful_markets:
            change = self._latest_change(
                changes_by_market[request.market],
                request.company_id,
            )
            verified = bool(
                finalizable
                and (
                    (
                        change is not None
                        and self._change_matches_database(
                            cursor,
                            verify_statements[request.market],
                            value_functions[request.market](change),
                        )
                    )
                    or (
                        change is None
                        and self._market_is_empty_in_database(
                            cursor,
                            empty_statements[request.market],
                            snapshot.match_id,
                            request.company_id,
                        )
                    )
                )
            )
            status_values[request.market].append(
                (
                    snapshot.match_id,
                    request.company_id,
                    verified,
                    change.seq if verified and change is not None else None,
                    1,
                )
            )
        return status_values

    @staticmethod
    def _match_is_finalizable(cursor: Any, match_id: int) -> bool:
        cursor.execute("SELECT to_regclass('match_basic_info')")
        relation = cursor.fetchone()
        if not relation or relation[0] is None:
            return False
        cursor.execute(
            """
            SELECT status_text = '完'
               AND CASE
                   WHEN scheduled_time ~
                       '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                   THEN scheduled_time::TIMESTAMP
                       <= (NOW() AT TIME ZONE 'Asia/Shanghai')
                           - INTERVAL '3 hours'
                   ELSE FALSE
               END
            FROM match_basic_info
            WHERE match_id = %s
            """,
            (match_id,),
        )
        result = cursor.fetchone()
        return bool(result and result[0])

    @staticmethod
    def _latest_change(changes: Sequence[Any], company_id: int) -> Optional[Any]:
        company_changes = [
            change for change in changes if change.company_id == company_id
        ]
        return max(company_changes, key=lambda change: change.seq, default=None)

    @staticmethod
    def _change_matches_database(
        cursor: Any,
        statement: str,
        values: Tuple[Any, ...],
    ) -> bool:
        cursor.execute(
            statement,
            values[3:] + values[:3] + values[:2],
        )
        result = cursor.fetchone()
        return bool(result and result[0])

    @staticmethod
    def _market_is_empty_in_database(
        cursor: Any,
        statement: str,
        match_id: int,
        company_id: int,
    ) -> bool:
        cursor.execute(statement, (match_id, company_id))
        result = cursor.fetchone()
        return bool(result and result[0])

    @classmethod
    def _handicap_values(cls, change: HandicapChange) -> Tuple[Any, ...]:
        return cls._common_values(change) + (
            change.home_odds,
            cls._movement_value(change.home_odds_movement),
            change.handicap_raw,
            change.handicap_value,
            cls._movement_value(change.handicap_movement),
            change.away_odds,
            cls._movement_value(change.away_odds_movement),
        )

    @classmethod
    def _one_x_two_values(cls, change: OneXTwoChange) -> Tuple[Any, ...]:
        return cls._common_values(change) + (
            change.home_win_odds,
            cls._movement_value(change.home_win_odds_movement),
            change.draw_odds,
            cls._movement_value(change.draw_odds_movement),
            change.away_win_odds,
            cls._movement_value(change.away_win_odds_movement),
        )

    @classmethod
    def _over_under_values(cls, change: OverUnderChange) -> Tuple[Any, ...]:
        return cls._common_values(change) + (
            change.over_odds,
            cls._movement_value(change.over_odds_movement),
            change.total_line_raw,
            change.total_line_value,
            cls._movement_value(change.total_line_movement),
            change.under_odds,
            cls._movement_value(change.under_odds_movement),
        )

    @staticmethod
    def _common_values(change: OddsChange) -> Tuple[Any, ...]:
        return (
            change.match_id,
            change.company_id,
            change.seq,
            change.match_minute,
            change.home_score,
            change.away_score,
            change.change_time,
            change.source_status,
            change.is_suspended,
        )

    @staticmethod
    def _movement_value(movement: Optional[Movement]) -> Optional[str]:
        return movement.value if movement is not None else None
