"""PostgreSQL persistence for Titan007 odds-change snapshots."""

import asyncio
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extensions import connection as Connection
from psycopg2.extras import execute_values

from .match_completion import REQUIRED_COMPANY_IDS, mark_matches_completed
from .models import (
    HandicapChange,
    Movement,
    OddsChange,
    OddsSnapshot,
    OneXTwoChange,
    OverUnderChange,
)
from .schema import load_migration


INITIALIZE_ODDS_TABLES = load_migration(
    "003_titan007_odds_changes.sql"
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


UPSERT_ODDS_FETCH_STATUS = """
INSERT INTO titan007_odds_fetch_status (
    match_id,
    company_id,
    handicap_completed,
    one_x_two_completed,
    over_under_completed,
    handicap_last_seq,
    one_x_two_last_seq,
    over_under_last_seq,
    verification_version,
    final_verified_at
)
VALUES %s
ON CONFLICT (match_id, company_id) DO UPDATE SET
    handicap_completed = EXCLUDED.handicap_completed,
    one_x_two_completed = EXCLUDED.one_x_two_completed,
    over_under_completed = EXCLUDED.over_under_completed,
    handicap_last_seq = EXCLUDED.handicap_last_seq,
    one_x_two_last_seq = EXCLUDED.one_x_two_last_seq,
    over_under_last_seq = EXCLUDED.over_under_last_seq,
    verification_version = EXCLUDED.verification_version,
    final_verified_at = CASE
        WHEN EXCLUDED.handicap_completed
         AND EXCLUDED.one_x_two_completed
         AND EXCLUDED.over_under_completed
        THEN NOW()
        ELSE NULL
    END,
    updated_at = NOW()
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
LEFT JOIN titan007_odds_fetch_status AS odds_status
  ON odds_status.match_id = status.match_id
WHERE status.crawl_status = '未完成'
GROUP BY status.match_id
HAVING COUNT(*) FILTER (
    WHERE odds_status.company_id IN (3, 4, 8, 24, 31, 47)
      AND odds_status.handicap_completed
      AND odds_status.one_x_two_completed
      AND odds_status.over_under_completed
      AND odds_status.verification_version = 1
) < 6
ORDER BY MAX(odds_status.updated_at) ASC NULLS FIRST, status.match_id
LIMIT %s
"""


COUNT_PENDING_MATCH_IDS = """
SELECT COUNT(*)
FROM (
    SELECT status.match_id
    FROM match_status AS status
    LEFT JOIN titan007_odds_fetch_status AS odds_status
      ON odds_status.match_id = status.match_id
    WHERE status.crawl_status = '未完成'
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


TOUCH_ODDS_ATTEMPT = """
INSERT INTO titan007_odds_fetch_status (match_id, company_id)
SELECT %s, company_id
FROM UNNEST(%s::INTEGER[]) AS company_id
ON CONFLICT (match_id, company_id) DO UPDATE SET
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

    async def touch_match_attempt(self, match_id: int) -> None:
        async with self._get_lock():
            await asyncio.to_thread(self._touch_match_attempt_sync, match_id)

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
                if fetch_status_values:
                    execute_values(
                        cursor,
                        UPSERT_ODDS_FETCH_STATUS,
                        fetch_status_values,
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

    def _touch_match_attempt_sync(self, match_id: int) -> None:
        if self._connection is None:
            raise RuntimeError("PostgresOddsStore is not initialized")
        with self._connection:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    TOUCH_ODDS_ATTEMPT,
                    (int(match_id), list(REQUIRED_COMPANY_IDS)),
                )

    def _fetch_status_values(
        self,
        cursor: Any,
        snapshot: OddsSnapshot,
    ) -> List[Tuple[Any, ...]]:
        finalizable = self._match_is_finalizable(cursor, snapshot.match_id)
        status_values: List[Tuple[Any, ...]] = []
        for company_id in snapshot.companies:
            handicap = self._latest_change(snapshot.handicap_changes, company_id)
            one_x_two = self._latest_change(snapshot.one_x_two_changes, company_id)
            over_under = self._latest_change(snapshot.over_under_changes, company_id)

            handicap_verified = bool(
                finalizable
                and (
                    (
                        handicap is not None
                        and self._change_matches_database(
                            cursor,
                            VERIFY_HANDICAP,
                            self._handicap_values(handicap),
                        )
                    )
                    or (
                        handicap is None
                        and self._market_is_empty_in_database(
                            cursor,
                            VERIFY_EMPTY_HANDICAP,
                            snapshot.match_id,
                            company_id,
                        )
                    )
                )
            )
            one_x_two_verified = bool(
                finalizable
                and (
                    (
                        one_x_two is not None
                        and self._change_matches_database(
                            cursor,
                            VERIFY_ONE_X_TWO,
                            self._one_x_two_values(one_x_two),
                        )
                    )
                    or (
                        one_x_two is None
                        and self._market_is_empty_in_database(
                            cursor,
                            VERIFY_EMPTY_ONE_X_TWO,
                            snapshot.match_id,
                            company_id,
                        )
                    )
                )
            )
            over_under_verified = bool(
                finalizable
                and (
                    (
                        over_under is not None
                        and self._change_matches_database(
                            cursor,
                            VERIFY_OVER_UNDER,
                            self._over_under_values(over_under),
                        )
                    )
                    or (
                        over_under is None
                        and self._market_is_empty_in_database(
                            cursor,
                            VERIFY_EMPTY_OVER_UNDER,
                            snapshot.match_id,
                            company_id,
                        )
                    )
                )
            )
            all_verified = (
                handicap_verified and one_x_two_verified and over_under_verified
            )
            status_values.append(
                (
                    snapshot.match_id,
                    company_id,
                    handicap_verified,
                    one_x_two_verified,
                    over_under_verified,
                    (
                        handicap.seq
                        if handicap_verified and handicap is not None
                        else None
                    ),
                    (
                        one_x_two.seq
                        if one_x_two_verified and one_x_two is not None
                        else None
                    ),
                    (
                        over_under.seq
                        if over_under_verified and over_under is not None
                        else None
                    ),
                    1,
                    datetime.now(timezone.utc) if all_verified else None,
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
