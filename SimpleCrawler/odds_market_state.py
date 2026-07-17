"""Persist the latest collection state for each Titan007 odds market page."""

import hashlib
import json
from typing import Any, Optional, Sequence


SUPPORTED_MARKETS = {"handicap", "one_x_two", "over_under"}

CREATE_ODDS_MARKET_STATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS titan007_odds_market_state (
    match_id BIGINT NOT NULL REFERENCES match_ids(match_id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    market TEXT NOT NULL
        CHECK (market IN ('handicap', 'one_x_two', 'over_under')),
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_success_at TIMESTAMPTZ,
    fetch_status TEXT NOT NULL
        CHECK (fetch_status IN ('待抓取', '成功', '失败')),
    row_count INTEGER CHECK (row_count >= 0),
    content_hash CHAR(64)
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    last_error TEXT,
    final_required BOOLEAN NOT NULL DEFAULT FALSE,
    final_success_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id, market),
    CHECK (
        fetch_status <> '成功'
        OR (
            last_success_at IS NOT NULL
            AND row_count IS NOT NULL
            AND content_hash IS NOT NULL
            AND last_error IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS titan007_odds_market_state_final_pending_idx
    ON titan007_odds_market_state (match_id, company_id, market)
    WHERE final_required AND final_success_at IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'titan007_odds_market_state'::regclass
          AND conname = 'titan007_odds_market_state_fetch_status_check'
          AND pg_get_constraintdef(oid) NOT LIKE '%待抓取%'
    ) THEN
        ALTER TABLE titan007_odds_market_state
            DROP CONSTRAINT titan007_odds_market_state_fetch_status_check;
        ALTER TABLE titan007_odds_market_state
            ADD CONSTRAINT titan007_odds_market_state_fetch_status_check
            CHECK (fetch_status IN ('待抓取', '成功', '失败'));
    END IF;
END
$$
"""

UPSERT_SUCCESS_SQL = """
INSERT INTO titan007_odds_market_state (
    match_id, company_id, market,
    last_attempt_at, last_success_at, fetch_status,
    row_count, content_hash, last_error
)
VALUES (%s, %s, %s, NOW(), NOW(), '成功', %s, %s, NULL)
ON CONFLICT (match_id, company_id, market) DO UPDATE SET
    last_attempt_at = NOW(),
    last_success_at = NOW(),
    fetch_status = '成功',
    row_count = EXCLUDED.row_count,
    content_hash = EXCLUDED.content_hash,
    last_error = NULL,
    updated_at = NOW()
"""

UPSERT_FAILURE_SQL = """
INSERT INTO titan007_odds_market_state (
    match_id, company_id, market,
    last_attempt_at, fetch_status, last_error
)
VALUES (%s, %s, %s, NOW(), '失败', %s)
ON CONFLICT (match_id, company_id, market) DO UPDATE SET
    last_attempt_at = NOW(),
    fetch_status = '失败',
    last_error = EXCLUDED.last_error,
    updated_at = NOW()
"""

UPSERT_FINAL_SUCCESS_SQL = """
INSERT INTO titan007_odds_market_state (
    match_id, company_id, market,
    last_attempt_at, last_success_at, fetch_status,
    row_count, content_hash, last_error,
    final_required, final_success_at
)
VALUES (%s, %s, %s, NOW(), NOW(), '成功', %s, %s, NULL, FALSE, NOW())
ON CONFLICT (match_id, company_id, market) DO UPDATE SET
    last_attempt_at = NOW(),
    last_success_at = NOW(),
    fetch_status = '成功',
    row_count = EXCLUDED.row_count,
    content_hash = EXCLUDED.content_hash,
    last_error = NULL,
    final_required = FALSE,
    final_success_at = NOW(),
    updated_at = NOW()
"""

UPSERT_FINAL_FAILURE_SQL = """
INSERT INTO titan007_odds_market_state (
    match_id, company_id, market,
    last_attempt_at, fetch_status, last_error,
    final_required, final_success_at
)
VALUES (%s, %s, %s, NOW(), '失败', %s, TRUE, NULL)
ON CONFLICT (match_id, company_id, market) DO UPDATE SET
    last_attempt_at = NOW(),
    fetch_status = '失败',
    last_error = EXCLUDED.last_error,
    final_required = TRUE,
    final_success_at = NULL,
    updated_at = NOW()
"""

PREPARE_FINAL_SNAPSHOT_SQL = """
INSERT INTO titan007_odds_market_state (
    match_id, company_id, market,
    fetch_status, last_error, final_required
)
VALUES (%s, %s, %s, '待抓取', NULL, TRUE)
ON CONFLICT (match_id, company_id, market) DO UPDATE SET
    final_required = TRUE,
    updated_at = NOW()
WHERE titan007_odds_market_state.final_success_at IS NULL
  AND NOT titan007_odds_market_state.final_required
"""


def ensure_market_state_schema(cursor: Any) -> None:
    cursor.execute(CREATE_ODDS_MARKET_STATE_TABLE_SQL)


def record_market_result(
    cursor: Any,
    *,
    match_id: int,
    company_id: int,
    market: str,
    rows: Optional[Sequence[Sequence[Any]]] = None,
    error: Optional[str] = None,
    final: bool = False,
) -> Optional[str]:
    """Record exactly one successful parsed page or one failed page attempt."""

    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"unsupported market: {market}")
    if (rows is None) == (error is None):
        raise ValueError("provide exactly one of rows or error")

    if rows is not None:
        content_hash = _content_hash(rows)
        cursor.execute(
            UPSERT_FINAL_SUCCESS_SQL if final else UPSERT_SUCCESS_SQL,
            (match_id, company_id, market, len(rows), content_hash),
        )
        return content_hash

    cursor.execute(
        UPSERT_FINAL_FAILURE_SQL if final else UPSERT_FAILURE_SQL,
        (match_id, company_id, market, str(error)),
    )
    return None


def prepare_final_snapshot(
    cursor: Any,
    *,
    match_id: int,
    company_ids: Sequence[int],
    markets: Sequence[str],
) -> None:
    pages = [
        (match_id, company_id, market)
        for company_id in company_ids
        for market in markets
    ]
    cursor.executemany(PREPARE_FINAL_SNAPSHOT_SQL, pages)


def load_pending_final_pages(
    cursor: Any,
    match_id: int,
    company_ids: Sequence[int],
) -> list[tuple[int, str]]:
    cursor.execute(
        """
        SELECT company_id, market
        FROM titan007_odds_market_state
        WHERE match_id = %s
          AND company_id = ANY(%s)
          AND final_required
          AND final_success_at IS NULL
        ORDER BY company_id, market
        """,
        (match_id, list(company_ids)),
    )
    return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def final_snapshot_complete(
    cursor: Any,
    match_id: int,
    company_ids: Sequence[int],
    market_count: int,
) -> bool:
    cursor.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(
                BOOL_AND(
                    NOT final_required
                    AND final_success_at IS NOT NULL
                ),
                FALSE
            )
        FROM titan007_odds_market_state
        WHERE match_id = %s
          AND company_id = ANY(%s)
        """,
        (match_id, list(company_ids)),
    )
    row = cursor.fetchone()
    expected = len(company_ids) * market_count
    return bool(row and int(row[0]) == expected and row[1])


def _content_hash(rows: Sequence[Sequence[Any]]) -> str:
    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
