#!/usr/bin/env python3
"""Push newly qualifying, not-started odds markets to a WeCom group."""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv
from psycopg2.extensions import connection as Connection

from simple_crawler.monitoring import format_round_match_count


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
TASK_PREFIX = "[企业微信通知]"
DEFAULT_TIMEOUT_SECONDS = 10.0
RUN_LOCK_KEYS = (2607, 47)
INITIALIZATION_LOCK_KEYS = (2607, 48)
MARKET_LABELS = {
    "over_under": "大小球（大球）",
    "handicap_home": "让球盘（主队）",
    "handicap_away": "让球盘（客队）",
}

CREATE_NOTIFICATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public.wecom_match_market_push_state (
    state_key TEXT PRIMARY KEY,
    initialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (state_key = 'match_market_baseline')
);

CREATE TABLE IF NOT EXISTS public.wecom_match_market_pushes (
    match_id BIGINT NOT NULL,
    market_type TEXT NOT NULL
        CHECK (market_type IN ('over_under', 'handicap_home', 'handicap_away')),
    push_status TEXT NOT NULL
        CHECK (push_status IN ('baseline', 'pending', 'sent', 'failed', 'expired')),
    company_count BIGINT NOT NULL CHECK (company_count >= 3),
    line_value NUMERIC(6, 2),
    league TEXT,
    scheduled_time TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    PRIMARY KEY (match_id, market_type)
)
"""

BASELINE_SQL = """
INSERT INTO public.wecom_match_market_pushes (
    match_id,
    market_type,
    push_status,
    company_count,
    line_value,
    league,
    scheduled_time,
    home_team,
    away_team
)
SELECT
    summary.match_id,
    summary.market_type,
    'baseline',
    summary.company_count,
    summary.line_value,
    details.league,
    details.scheduled_time,
    details.home_team,
    details.away_team
FROM public.match_odds_filter_market_summary AS summary
JOIN public.match_details AS details USING (match_id)
WHERE details.status_text = '未开始'
ON CONFLICT (match_id, market_type) DO NOTHING
"""

DISCOVER_SQL = """
INSERT INTO public.wecom_match_market_pushes (
    match_id,
    market_type,
    push_status,
    company_count,
    line_value,
    league,
    scheduled_time,
    home_team,
    away_team
)
SELECT
    summary.match_id,
    summary.market_type,
    'pending',
    summary.company_count,
    summary.line_value,
    details.league,
    details.scheduled_time,
    details.home_team,
    details.away_team
FROM public.match_odds_filter_market_summary AS summary
JOIN public.match_details AS details USING (match_id)
JOIN public.match_ids AS ids USING (match_id)
CROSS JOIN public.wecom_match_market_push_state AS state
WHERE details.status_text = '未开始'
  AND state.state_key = 'match_market_baseline'
  AND ids.created_at > state.initialized_at
ON CONFLICT (match_id, market_type) DO NOTHING
"""

EXPIRE_SQL = """
UPDATE public.wecom_match_market_pushes AS pushes
SET push_status = 'expired'
WHERE pushes.push_status IN ('pending', 'failed')
  AND NOT EXISTS (
      SELECT 1
      FROM public.match_details AS details
      WHERE details.match_id = pushes.match_id
        AND details.status_text = '未开始'
  )
"""

LOAD_DELIVERIES_SQL = """
SELECT
    match_id,
    market_type,
    company_count,
    line_value,
    league,
    scheduled_time,
    home_team,
    away_team
FROM public.wecom_match_market_pushes
WHERE push_status IN ('pending', 'failed')
ORDER BY match_id, market_type
"""


class WeComDeliveryError(RuntimeError):
    """Raised when a WeCom webhook does not confirm delivery."""


@dataclass(frozen=True)
class PushRecord:
    match_id: int
    market_type: str
    company_count: int
    line_value: Optional[Decimal]
    league: Optional[str]
    scheduled_time: str
    home_team: str
    away_team: str


@dataclass(frozen=True)
class PreparationResult:
    initialized: bool
    recorded_count: int


def positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是数字") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return parsed


def validate_webhook_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("SIMPLE_CRAWLER_WECOM_WEBHOOK_URL 必须是 HTTPS URL")
    return value


def ensure_notification_schema(connection: Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(CREATE_NOTIFICATION_SCHEMA_SQL)


def acquire_run_lock(connection: Connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            RUN_LOCK_KEYS,
        )
        return bool(cursor.fetchone()[0])


def prepare_notifications(connection: Connection) -> PreparationResult:
    """Create the first baseline or discover later not-started markets."""

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            INITIALIZATION_LOCK_KEYS,
        )
        cursor.execute(
            """
            SELECT 1
            FROM public.wecom_match_market_push_state
            WHERE state_key = 'match_market_baseline'
            """
        )
        initialized = cursor.fetchone() is not None
        if not initialized:
            cursor.execute(BASELINE_SQL)
            recorded_count = cursor.rowcount
            cursor.execute(
                """
                INSERT INTO public.wecom_match_market_push_state (state_key)
                VALUES ('match_market_baseline')
                ON CONFLICT (state_key) DO NOTHING
                """
            )
            return PreparationResult(False, recorded_count)

        cursor.execute(EXPIRE_SQL)
        cursor.execute(DISCOVER_SQL)
        return PreparationResult(True, cursor.rowcount)


def load_deliveries(connection: Connection) -> List[PushRecord]:
    with connection.cursor() as cursor:
        cursor.execute(LOAD_DELIVERIES_SQL)
        return [
            PushRecord(
                match_id=int(row[0]),
                market_type=str(row[1]),
                company_count=int(row[2]),
                line_value=row[3],
                league=row[4],
                scheduled_time=str(row[5]),
                home_team=str(row[6]),
                away_team=str(row[7]),
            )
            for row in cursor.fetchall()
        ]


def group_deliveries(records: Sequence[PushRecord]) -> Dict[int, List[PushRecord]]:
    grouped: Dict[int, List[PushRecord]] = defaultdict(list)
    for record in records:
        grouped[record.match_id].append(record)
    return dict(grouped)


def format_line_value(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def plain_text(value: object) -> str:
    text = " ".join(str(value).split())
    clean = text.translate(str.maketrans("", "", "#*[]`>"))
    return " ".join(clean.split())


def build_message(records: Sequence[PushRecord]) -> str:
    if not records:
        raise ValueError("推送消息至少需要一个市场")
    match = records[0]
    matchup = f"{plain_text(match.home_team)} - {plain_text(match.away_team)}"
    lines = [
        "赔率筛选命中",
        f"联赛: {plain_text(match.league or '-')}",
        f"对阵: {matchup}",
        f"开赛时间: {plain_text(match.scheduled_time)}",
        f"比赛ID: {match.match_id}",
        "",
        "命中市场",
    ]
    for record in records:
        lines.append(
            f"{MARKET_LABELS[record.market_type]}: "
            f"{record.company_count} 家, 最大盘口 "
            f"{format_line_value(record.line_value)}"
        )
    link = (
        "https://live.nowscore.com/odds/3in1Odds.aspx?companyid=3"
        f"&id={match.match_id}"
    )
    lines.extend(("", "赔率链接", link))
    return "\n".join(lines)


def send_wecom_text(
    webhook_url: str,
    content: str,
    timeout_seconds: float,
) -> None:
    payload = json.dumps(
        {"msgtype": "text", "text": {"content": content}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw_response = response.read(16_384)
    except urllib.error.HTTPError as error:
        raise WeComDeliveryError(
            f"企业微信返回 HTTP {error.code}"
        ) from None
    except (OSError, urllib.error.URLError) as error:
        reason = getattr(error, "reason", None)
        detail = type(reason or error).__name__
        raise WeComDeliveryError(f"企业微信连接失败：{detail}") from None
    try:
        result = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WeComDeliveryError("企业微信返回了无效 JSON") from None
    if not isinstance(result, dict) or result.get("errcode") != 0:
        code = result.get("errcode", "unknown") if isinstance(result, dict) else "unknown"
        message = (
            result.get("errmsg", "未知错误")
            if isinstance(result, dict)
            else "未知错误"
        )
        raise WeComDeliveryError(f"企业微信拒绝消息：{code} {message}")


def mark_delivery(
    connection: Connection,
    match_id: int,
    market_types: Sequence[str],
    *,
    error: Optional[str],
) -> None:
    with connection.cursor() as cursor:
        if error is None:
            cursor.execute(
                """
                UPDATE public.wecom_match_market_pushes
                SET push_status = 'sent',
                    last_attempt_at = NOW(),
                    sent_at = NOW(),
                    attempt_count = attempt_count + 1,
                    last_error = NULL
                WHERE match_id = %s
                  AND market_type = ANY(%s)
                  AND push_status IN ('pending', 'failed')
                """,
                (match_id, list(market_types)),
            )
        else:
            cursor.execute(
                """
                UPDATE public.wecom_match_market_pushes
                SET push_status = 'failed',
                    last_attempt_at = NOW(),
                    attempt_count = attempt_count + 1,
                    last_error = %s
                WHERE match_id = %s
                  AND market_type = ANY(%s)
                  AND push_status IN ('pending', 'failed')
                """,
                (error, match_id, list(market_types)),
            )


def run_once(
    database_url: str,
    webhook_url: str,
    timeout_seconds: float,
) -> tuple[int, int, bool]:
    sent_count = 0
    failed_count = 0
    with psycopg2.connect(database_url) as connection:
        ensure_notification_schema(connection)
        connection.commit()
        if not acquire_run_lock(connection):
            print(f"{TASK_PREFIX} 已有通知任务在运行，本轮跳过。")
            return 0, 0, False

        preparation = prepare_notifications(connection)
        connection.commit()
        if not preparation.initialized:
            print(
                f"{TASK_PREFIX} 首次基线已建立："
                f"{preparation.recorded_count} 条比赛市场，本轮不推送。"
            )
            return 0, 0, True

        grouped = group_deliveries(load_deliveries(connection))
        for match_id, records in grouped.items():
            market_types = [record.market_type for record in records]
            try:
                send_wecom_text(
                    webhook_url,
                    build_message(records),
                    timeout_seconds,
                )
            except WeComDeliveryError as error:
                message = str(error)
                mark_delivery(
                    connection,
                    match_id,
                    market_types,
                    error=message,
                )
                connection.commit()
                failed_count += 1
                print(f"{TASK_PREFIX} 比赛 {match_id} 推送失败：{message}")
                continue
            mark_delivery(connection, match_id, market_types, error=None)
            connection.commit()
            sent_count += 1
            print(
                f"{TASK_PREFIX} 比赛 {match_id} 推送成功："
                f"{'、'.join(market_types)}"
            )
        return sent_count, failed_count, True


def main() -> int:
    load_dotenv(ENV_FILE)
    webhook_text = os.environ.get("SIMPLE_CRAWLER_WECOM_WEBHOOK_URL", "").strip()
    if not webhook_text:
        print(f"{TASK_PREFIX} 未配置 Webhook，本轮跳过。")
        print(format_round_match_count(TASK_PREFIX, 0))
        return 0
    database_url = os.environ.get("SIMPLE_CRAWLER_DATABASE_URL", "").strip()
    if not database_url:
        print(f"{TASK_PREFIX} 未配置 SIMPLE_CRAWLER_DATABASE_URL。", file=sys.stderr)
        return 1
    try:
        webhook_url = validate_webhook_url(webhook_text)
        timeout_seconds = positive_float(
            os.environ.get(
                "SIMPLE_CRAWLER_WECOM_TIMEOUT_SECONDS",
                str(DEFAULT_TIMEOUT_SECONDS),
            ),
            "SIMPLE_CRAWLER_WECOM_TIMEOUT_SECONDS",
        )
        sent_count, failed_count, acquired = run_once(
            database_url,
            webhook_url,
            timeout_seconds,
        )
    except (ValueError, psycopg2.Error) as error:
        message = str(error).splitlines()[0]
        print(f"{TASK_PREFIX} 任务失败：{message}", file=sys.stderr)
        return 1
    print(format_round_match_count(TASK_PREFIX, sent_count if acquired else 0))
    return 1 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
