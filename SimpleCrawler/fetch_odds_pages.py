#!/usr/bin/env python3
"""Fetch and store three Titan007 odds markets per company."""

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import psycopg2
from dotenv import load_dotenv
from simple_crawler.models import (
    HandicapChange,
    Movement,
    OddsChange,
    OneXTwoChange,
    OverUnderChange,
)
from simple_crawler.companies import COMPANY_IDS, company_label
from simple_crawler.odds_parser import Titan007OddsParser
from playwright.sync_api import Page
from playwright.async_api import Page as AsyncPage
from playwright.async_api import async_playwright
from psycopg2.extensions import connection as Connection
from psycopg2.extras import execute_values

try:
    from .concurrent_pages import async_proxy_lease, iter_bounded
    from .proxy_scheduler import ProxyClient
    from .crawl_status import env_active_crawl_statuses
except ImportError:
    from concurrent_pages import async_proxy_lease, iter_bounded
    from proxy_scheduler import ProxyClient
    from crawl_status import env_active_crawl_statuses


ENV_FILE = Path(__file__).with_name(".env")
DATABASE_ENV_NAME = "SIMPLE_CRAWLER_DATABASE_URL"
DEFAULT_BASE_URL = "https://vip.titan007.com/changeDetail/{endpoint}"
DEFAULT_COMPANY_IDS = list(COMPANY_IDS)
SUPPORTED_COMPANY_IDS = set(DEFAULT_COMPANY_IDS)
TASK_PREFIX = "[赔率]"
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
BLOCKED_RESOURCE_TYPES = {"stylesheet", "image", "media", "font"}
ERROR_MARKERS = (
    "access denied",
    "forbidden",
    "request blocked",
    "waf",
    "访问被拒绝",
    "禁止访问",
    "安全验证",
    "验证码",
)
EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_INDETERMINATE = 3
EXIT_MAJORITY_FAILURE = 10
MARKETS = {
    "handicap": ("handicap.aspx", "#odds2 table", "亚让"),
    "one_x_two": ("1x2.aspx", "#odds table", "胜平负"),
    "over_under": ("overunder.aspx", "#odds2 table", "进球数"),
}

CREATE_MATCH_IDS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS match_ids (
    match_id BIGINT PRIMARY KEY,
    crawl_status TEXT NOT NULL DEFAULT '未完成'
        CHECK (crawl_status IN ('未完成', '已完成', '暂停爬取', '异常')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""
CREATE_ODDS_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS titan007_handicap_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
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

CREATE TABLE IF NOT EXISTS titan007_1x2_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
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

CREATE TABLE IF NOT EXISTS titan007_over_under_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
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
)
"""

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

MarketChange = Union[HandicapChange, OneXTwoChange, OverUnderChange]


@dataclass(frozen=True)
class OddsPageJob:
    match_id: int
    company_id: int
    market: str


def env_float(
    parser: argparse.ArgumentParser,
    name: str,
    default: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        parser.error(f"{name} 必须是数字")
        raise AssertionError("argparse.error should exit")


def env_optional_int(
    parser: argparse.ArgumentParser,
    name: str,
) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        parser.error(f"{name} 必须是整数或留空")
        raise AssertionError("argparse.error should exit")


def env_int(
    parser: argparse.ArgumentParser,
    name: str,
    default: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        parser.error(f"{name} 必须是整数")
        raise AssertionError("argparse.error should exit")


def env_bool(
    parser: argparse.ArgumentParser,
    name: str,
    default: bool,
) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in TRUE_ENV_VALUES:
        return True
    if normalized in FALSE_ENV_VALUES:
        return False
    parser.error(f"{name} 必须是 true/false、1/0、yes/no 或 on/off")
    raise AssertionError("argparse.error should exit")


def env_company_ids(parser: argparse.ArgumentParser) -> List[int]:
    raw = os.environ.get("SIMPLE_CRAWLER_ODDS_COMPANY_IDS")
    if raw is None or not raw.strip():
        return DEFAULT_COMPANY_IDS.copy()
    try:
        company_ids = [int(value.strip()) for value in raw.split(",")]
    except ValueError:
        parser.error("SIMPLE_CRAWLER_ODDS_COMPANY_IDS 必须是逗号分隔的整数")
        raise AssertionError("argparse.error should exit")
    unsupported = [
        company_id
        for company_id in company_ids
        if company_id not in SUPPORTED_COMPANY_IDS
    ]
    if not company_ids or unsupported:
        parser.error(
            "SIMPLE_CRAWLER_ODDS_COMPANY_IDS 只支持 3,4,8,24,31,47"
        )
    return list(dict.fromkeys(company_ids))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取不同公司的三个 Titan007 赔率页面并存入数据库。"
    )
    parser.add_argument(
        "match_ids",
        nargs="*",
        type=int,
        help="指定比赛 ID；不传时读取数据库中的全部比赛 ID",
    )
    parser.add_argument(
        "--company-id",
        dest="company_ids",
        action="append",
        type=int,
        help="指定公司 ID，可重复传入；默认读取环境变量",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=env_optional_int(parser, "SIMPLE_CRAWLER_ODDS_MATCH_LIMIT"),
        help="本次最多处理多少场比赛",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=env_float(
            parser,
            "SIMPLE_CRAWLER_ODDS_TIMEOUT_SECONDS",
            15.0,
        ),
        help="赔率页面导航和等待表格时，每一步的超时秒数",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=env_int(
            parser,
            "SIMPLE_CRAWLER_ODDS_PAGE_CONCURRENCY",
            4,
        ),
        help="同时抓取的赔率页面数量（默认：4）",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "SIMPLE_CRAWLER_ODDS_BASE_URL",
            DEFAULT_BASE_URL,
        ),
        help="赔率变化页基础模板，必须包含 {endpoint}",
    )
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument(
        "--headed",
        dest="headed",
        action="store_true",
        help="显示浏览器窗口",
    )
    browser_mode.add_argument(
        "--headless",
        dest="headed",
        action="store_false",
        help="隐藏浏览器窗口",
    )
    parser.set_defaults(
        headed=env_bool(parser, "SIMPLE_CRAWLER_HEADED", False)
    )
    args = parser.parse_args(argv)
    args.active_crawl_statuses = env_active_crawl_statuses(parser)

    configured_companies = env_company_ids(parser)
    args.company_ids = list(
        dict.fromkeys(args.company_ids or configured_companies)
    )
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.concurrency <= 0:
        parser.error("--concurrency 必须大于 0")
    if "{endpoint}" not in args.base_url:
        parser.error("--base-url 必须包含 {endpoint}")
    if any(match_id <= 0 for match_id in args.match_ids):
        parser.error("比赛 ID 必须是正整数")
    if any(
        company_id not in SUPPORTED_COMPANY_IDS
        for company_id in args.company_ids
    ):
        parser.error("公司 ID 只支持 3、4、8、24、31、47")
    return args


def ensure_schema(connection: Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(CREATE_MATCH_IDS_TABLE_SQL)
        cursor.execute(CREATE_ODDS_TABLES_SQL)
    connection.commit()


def select_match_ids(
    connection: Connection,
    requested_ids: Sequence[int],
    limit: Optional[int],
    active_crawl_statuses: Sequence[str],
) -> List[int]:
    with connection.cursor() as cursor:
        if requested_ids:
            unique_ids = list(dict.fromkeys(requested_ids))
            cursor.executemany(
                """
                INSERT INTO match_ids (match_id)
                VALUES (%s)
                ON CONFLICT (match_id) DO NOTHING
                """,
                [(match_id,) for match_id in unique_ids],
            )
            connection.commit()
            cursor.execute(
                """
                SELECT match_id
                FROM match_ids
                WHERE match_id = ANY(%s)
                  AND crawl_status = ANY(%s)
                """,
                (unique_ids, list(active_crawl_statuses)),
            )
            allowed_ids = {row[0] for row in cursor.fetchall()}
            selected_ids = [
                match_id for match_id in unique_ids if match_id in allowed_ids
            ]
            return selected_ids[:limit]

        statement = """
            SELECT match_id
            FROM match_ids
            WHERE crawl_status = ANY(%s)
            ORDER BY match_id
        """
        parameters: Tuple[object, ...] = (list(active_crawl_statuses),)
        if limit is not None:
            statement += " LIMIT %s"
            parameters += (limit,)
        cursor.execute(statement, parameters)
        return [row[0] for row in cursor.fetchall()]


def build_url(
    base_url: str,
    match_id: int,
    company_id: int,
    market: str,
) -> str:
    endpoint = MARKETS[market][0]
    return (
        base_url.format(endpoint=endpoint)
        + f"?id={match_id}&companyid={company_id}&l=0"
    )


def block_unneeded_resources(route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


async def block_unneeded_resources_async(route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


def fetch_page_rows(
    page: Page,
    url: str,
    selector: str,
    timeout_seconds: float,
) -> List[Dict[str, Any]]:
    response = page.goto(
        url,
        wait_until="commit",
        timeout=int(timeout_seconds * 1000),
    )
    if response is None or response.status >= 400:
        status = "无响应" if response is None else response.status
        raise RuntimeError(f"赔率页面返回 HTTP {status}")

    page.wait_for_function(
        """selector => {
            const text = `${document.title} ${document.body?.innerText || ''}`
                .toLowerCase();
            const errors = [
                'access denied', 'forbidden', 'request blocked', 'waf',
                '访问被拒绝', '禁止访问', '安全验证', '验证码'
            ];
            return errors.some(marker => text.includes(marker))
                || Boolean(document.querySelector(selector))
                || Boolean(document.querySelector('#odds, #odds2'))
                || Boolean(document.querySelector(
                    'a[href*="handicap.aspx"], '
                    + 'a[href*="1x2.aspx"], '
                    + 'a[href*="overunder.aspx"]'
                ));
        }""",
        arg=selector,
        timeout=int(timeout_seconds * 1000),
    )
    state = page.evaluate(
        """selector => ({
            title: document.title || '',
            bodyText: document.body?.innerText || '',
            hasExpectedTable: Boolean(document.querySelector(selector)),
            hasMarketShell: Boolean(document.querySelector('#odds, #odds2')),
            hasMarketNavigation: Boolean(document.querySelector(
                'a[href*="handicap.aspx"], '
                + 'a[href*="1x2.aspx"], '
                + 'a[href*="overunder.aspx"]'
            ))
        })""",
        selector,
    )
    if not validate_page_state(state):
        return []

    return page.locator(selector).evaluate(
        """table => Array.from(table.rows).slice(1).map(row => ({
            cells: Array.from(row.cells).map(cell => {
                const colored = cell.querySelector('font[color]');
                return {
                    text: (cell.innerText || '').replace(/\s+/g, ' ').trim(),
                    colSpan: cell.colSpan || 1,
                    color: colored ? (colored.getAttribute('color') || '') : ''
                };
            })
        }))"""
    )


async def fetch_page_rows_async(
    page: AsyncPage,
    url: str,
    selector: str,
    timeout_seconds: float,
) -> List[Dict[str, Any]]:
    response = await page.goto(
        url,
        wait_until="commit",
        timeout=int(timeout_seconds * 1000),
    )
    if response is None or response.status >= 400:
        status = "无响应" if response is None else response.status
        raise RuntimeError(f"赔率页面返回 HTTP {status}")

    await page.wait_for_function(
        """selector => {
            const text = `${document.title} ${document.body?.innerText || ''}`
                .toLowerCase();
            const errors = [
                'access denied', 'forbidden', 'request blocked', 'waf',
                '访问被拒绝', '禁止访问', '安全验证', '验证码'
            ];
            return errors.some(marker => text.includes(marker))
                || Boolean(document.querySelector(selector))
                || Boolean(document.querySelector('#odds, #odds2'))
                || Boolean(document.querySelector(
                    'a[href*="handicap.aspx"], '
                    + 'a[href*="1x2.aspx"], '
                    + 'a[href*="overunder.aspx"]'
                ));
        }""",
        arg=selector,
        timeout=int(timeout_seconds * 1000),
    )
    state = await page.evaluate(
        """selector => ({
            title: document.title || '',
            bodyText: document.body?.innerText || '',
            hasExpectedTable: Boolean(document.querySelector(selector)),
            hasMarketShell: Boolean(document.querySelector('#odds, #odds2')),
            hasMarketNavigation: Boolean(document.querySelector(
                'a[href*="handicap.aspx"], '
                + 'a[href*="1x2.aspx"], '
                + 'a[href*="overunder.aspx"]'
            ))
        })""",
        selector,
    )
    if not validate_page_state(state):
        return []

    return await page.locator(selector).evaluate(
        """table => Array.from(table.rows).slice(1).map(row => ({
            cells: Array.from(row.cells).map(cell => {
                const colored = cell.querySelector('font[color]');
                return {
                    text: (cell.innerText || '').replace(/\s+/g, ' ').trim(),
                    colSpan: cell.colSpan || 1,
                    color: colored ? (colored.getAttribute('color') || '') : ''
                };
            })
        }))"""
    )


def validate_page_state(state: Dict[str, Any]) -> bool:
    """Return true for a data table, false for a valid empty market page."""

    text = f"{state.get('title', '')} {state.get('bodyText', '')}".lower()
    if any(marker in text for marker in ERROR_MARKERS):
        raise RuntimeError("赔率页面是拦截页或错误页")
    if state.get("hasExpectedTable"):
        return True
    if state.get("hasMarketShell") or state.get("hasMarketNavigation"):
        return False
    raise RuntimeError("赔率页面缺少预期的市场结构")


def movement_value(movement: Optional[Movement]) -> Optional[str]:
    return movement.value if movement is not None else None


def common_values(change: OddsChange) -> Tuple[Any, ...]:
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


def change_values(change: MarketChange) -> Tuple[Any, ...]:
    if isinstance(change, HandicapChange):
        return common_values(change) + (
            change.home_odds,
            movement_value(change.home_odds_movement),
            change.handicap_raw,
            change.handicap_value,
            movement_value(change.handicap_movement),
            change.away_odds,
            movement_value(change.away_odds_movement),
        )
    if isinstance(change, OneXTwoChange):
        return common_values(change) + (
            change.home_win_odds,
            movement_value(change.home_win_odds_movement),
            change.draw_odds,
            movement_value(change.draw_odds_movement),
            change.away_win_odds,
            movement_value(change.away_win_odds_movement),
        )
    return common_values(change) + (
        change.over_odds,
        movement_value(change.over_odds_movement),
        change.total_line_raw,
        change.total_line_value,
        movement_value(change.total_line_movement),
        change.under_odds,
        movement_value(change.under_odds_movement),
    )


def save_market_changes(
    connection: Connection,
    market: str,
    changes: Sequence[MarketChange],
) -> None:
    if changes:
        statements = {
            "handicap": UPSERT_HANDICAP,
            "one_x_two": UPSERT_ONE_X_TWO,
            "over_under": UPSERT_OVER_UNDER,
        }
        with connection.cursor() as cursor:
            execute_values(
                cursor,
                statements[market],
                [change_values(change) for change in changes],
            )
    connection.commit()


def result_exit_code(succeeded: int, failed: int) -> int:
    if failed > succeeded:
        return EXIT_MAJORITY_FAILURE
    if failed:
        return EXIT_PARTIAL_FAILURE
    return EXIT_SUCCESS


async def crawl_odds_pages(
    connection: Connection,
    match_ids: Sequence[int],
    args: argparse.Namespace,
) -> Tuple[int, int, int]:
    succeeded = 0
    failed = 0
    stored_rows = 0
    jobs = [
        OddsPageJob(match_id, company_id, market)
        for match_id in match_ids
        for company_id in args.company_ids
        for market in MARKETS
    ]
    proxy_client = ProxyClient.from_env()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        try:
            async def fetch(job: OddsPageJob) -> List[MarketChange]:
                _, selector, _ = MARKETS[job.market]
                minimum_lifetime = min(
                    args.timeout + 2,
                    proxy_client.ttl_seconds - 1,
                )
                async with async_proxy_lease(
                    proxy_client,
                    min_remaining_seconds=minimum_lifetime,
                ) as proxy:
                    context = await browser.new_context(
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        proxy=proxy.playwright_options(),
                    )
                    try:
                        page = await context.new_page()
                        await page.route(
                            "**/*",
                            block_unneeded_resources_async,
                        )
                        rows = await fetch_page_rows_async(
                            page,
                            build_url(
                                args.base_url,
                                job.match_id,
                                job.company_id,
                                job.market,
                            ),
                            selector,
                            args.timeout,
                        )
                        return Titan007OddsParser.parse_rows(
                            job.market,
                            rows,
                            match_id=job.match_id,
                            company_id=job.company_id,
                        )
                    finally:
                        await context.close()

            async for outcome in iter_bounded(
                jobs,
                args.concurrency,
                fetch,
            ):
                job = outcome.job
                label = MARKETS[job.market][2]
                if outcome.error is not None:
                    failed += 1
                    connection.rollback()
                    print(
                        f"{TASK_PREFIX} {job.match_id} | "
                        f"{company_label(job.company_id)} | "
                        f"{label}失败：{outcome.error}",
                        file=sys.stderr,
                    )
                    continue
                changes = outcome.result
                if changes is None:
                    failed += 1
                    print(
                        f"{TASK_PREFIX} {job.match_id} | "
                        f"{company_label(job.company_id)} | "
                        f"{label}失败：没有返回解析结果",
                        file=sys.stderr,
                    )
                    continue
                try:
                    save_market_changes(connection, job.market, changes)
                except Exception as error:
                    failed += 1
                    connection.rollback()
                    print(
                        f"{TASK_PREFIX} {job.match_id} | "
                        f"{company_label(job.company_id)} | "
                        f"{label}写入失败：{error}",
                        file=sys.stderr,
                    )
                else:
                    succeeded += 1
                    stored_rows += len(changes)
                    print(
                        f"{TASK_PREFIX} {job.match_id} | "
                        f"{company_label(job.company_id)} | "
                        f"{label} | {len(changes)} 条"
                    )
        finally:
            await browser.close()
    return succeeded, failed, stored_rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv(ENV_FILE)
    args = parse_args(argv)
    database_url = os.environ.get(DATABASE_ENV_NAME)
    if not database_url:
        print(
            f"{TASK_PREFIX} 错误：请在 {ENV_FILE} 中配置 "
            f"{DATABASE_ENV_NAME}。",
            file=sys.stderr,
        )
        return EXIT_INDETERMINATE

    connection: Optional[Connection] = None
    try:
        connection = psycopg2.connect(database_url)
        ensure_schema(connection)
        match_ids = select_match_ids(
            connection,
            args.match_ids,
            args.limit,
            args.active_crawl_statuses,
        )
    except psycopg2.Error as error:
        if connection is not None:
            connection.close()
        print(f"{TASK_PREFIX} 数据库访问失败：{error}", file=sys.stderr)
        return EXIT_INDETERMINATE

    if not match_ids:
        connection.close()
        print(f"{TASK_PREFIX} 数据库中没有比赛 ID。", file=sys.stderr)
        return EXIT_SUCCESS

    try:
        succeeded, failed, stored_rows = asyncio.run(
            crawl_odds_pages(connection, match_ids, args)
        )
    except Exception as error:
        print(f"{TASK_PREFIX} 赔率抓取中断：{error}", file=sys.stderr)
        return EXIT_INDETERMINATE
    finally:
        connection.close()

    print(
        f"{TASK_PREFIX} 赔率抓取完成：成功页面 {succeeded}，"
        f"失败页面 {failed}，"
        f"本轮读取 {stored_rows} 条赔率变动记录。",
        file=sys.stderr,
    )
    return result_exit_code(succeeded, failed)


if __name__ == "__main__":
    raise SystemExit(main())
