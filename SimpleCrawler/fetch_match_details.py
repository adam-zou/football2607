#!/usr/bin/env python3
"""Fetch Titan007 match details and store them in PostgreSQL."""

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg2
from dotenv import load_dotenv
from playwright.async_api import Browser as AsyncBrowser
from playwright.async_api import Page as AsyncPage
from playwright.async_api import async_playwright
from psycopg2.extensions import connection as Connection

try:
    from .concurrent_pages import async_proxy_lease, iter_bounded
    from .proxy_scheduler import ProxyClient
    from .crawl_status import env_active_crawl_statuses
except ImportError:
    from concurrent_pages import async_proxy_lease, iter_bounded
    from proxy_scheduler import ProxyClient
    from crawl_status import env_active_crawl_statuses


DEFAULT_URL_TEMPLATE = "https://live.titan007.com/detail/{match_id}sb.htm"
HEADER_SELECTOR = "#header .analyhead"
ENV_FILE = Path(__file__).with_name(".env")
DATABASE_ENV_NAME = "SIMPLE_CRAWLER_DATABASE_URL"
TASK_PREFIX = "[比赛详情]"
MAX_DETAIL_FETCH_ATTEMPTS = 3
BLOCKED_RESOURCE_TYPES = {"stylesheet", "image", "media", "font"}
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}

CREATE_MATCH_IDS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS match_ids (
    match_id BIGINT PRIMARY KEY,
    crawl_status TEXT NOT NULL DEFAULT '未完成'
        CHECK (crawl_status IN ('未完成', '已完成', '暂停爬取', '异常')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""
CREATE_MATCH_DETAILS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS match_details (
    match_id BIGINT PRIMARY KEY REFERENCES match_ids(match_id) ON DELETE CASCADE,
    league TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    home_score SMALLINT,
    away_score SMALLINT,
    status_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


@dataclass(frozen=True)
class MatchDetail:
    match_id: int
    league: str
    home_team: str
    away_team: str
    scheduled_time: str
    home_score: Optional[int]
    away_score: Optional[int]
    status_text: str


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="根据比赛 ID 获取 Titan007 详情并写入数据库。"
    )
    parser.add_argument(
        "match_ids",
        nargs="*",
        type=int,
        help="指定比赛 ID；不传时抓取数据库中的全部比赛 ID",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=env_optional_int(parser, "SIMPLE_CRAWLER_DETAIL_LIMIT"),
        help="本次最多处理多少场；默认处理全部待抓取比赛",
    )
    parser.add_argument(
        "--url-template",
        default=os.environ.get(
            "SIMPLE_CRAWLER_DETAIL_URL_TEMPLATE",
            DEFAULT_URL_TEMPLATE,
        ),
        help="详情页地址模板，必须包含 {match_id}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=env_float(
            parser,
            "SIMPLE_CRAWLER_DETAIL_TIMEOUT_SECONDS",
            15.0,
        ),
        help="每个详情页的超时秒数（默认：15）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=env_int(
            parser,
            "SIMPLE_CRAWLER_DETAIL_CONCURRENCY",
            2,
        ),
        help="同时抓取的详情页数量（默认：2）",
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

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.concurrency <= 0:
        parser.error("--concurrency 必须大于 0")
    if "{match_id}" not in args.url_template:
        parser.error("--url-template 必须包含 {match_id}")
    if any(match_id <= 0 for match_id in args.match_ids):
        parser.error("比赛 ID 必须是正整数")
    return args


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


def ensure_schema(connection: Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(CREATE_MATCH_IDS_TABLE_SQL)
        cursor.execute(CREATE_MATCH_DETAILS_TABLE_SQL)
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
            SELECT ids.match_id
            FROM match_ids AS ids
            LEFT JOIN match_details AS details USING (match_id)
            WHERE ids.crawl_status = ANY(%s)
              AND (
                  details.match_id IS NULL
                  OR (
                      details.status_text <> '完'
                      AND details.updated_at
                          <= NOW() - INTERVAL '1 minute'
                      AND (
                          (
                              details.scheduled_time ~
                                  '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                              AND details.scheduled_time::TIMESTAMP
                                  AT TIME ZONE 'Asia/Shanghai'
                                  >= NOW() - INTERVAL '4 hours'
                              AND details.scheduled_time::TIMESTAMP
                                  AT TIME ZONE 'Asia/Shanghai'
                                  <= NOW() + INTERVAL '30 minutes'
                          )
                          OR details.scheduled_time !~
                              '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                      )
                  )
              )
            ORDER BY (details.match_id IS NULL) DESC,
                     CASE
                         WHEN details.scheduled_time ~
                             '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                         THEN details.scheduled_time::TIMESTAMP
                             AT TIME ZONE 'Asia/Shanghai'
                     END NULLS LAST,
                     ids.match_id
        """
        parameters: Tuple[object, ...] = (list(active_crawl_statuses),)
        if limit is not None:
            statement += " LIMIT %s"
            parameters += (limit,)
        cursor.execute(statement, parameters)
        return [row[0] for row in cursor.fetchall()]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_scores(values: Any) -> Tuple[Optional[int], Optional[int]]:
    if not isinstance(values, list) or len(values) != 2:
        return None, None
    cleaned = [clean_text(value) for value in values]
    if not all(re.fullmatch(r"\d+", value) for value in cleaned):
        return None, None
    return int(cleaned[0]), int(cleaned[1])


def parse_detail(raw: Dict[str, Any]) -> MatchDetail:
    league = clean_text(raw.get("league"))
    home_team = clean_text(raw.get("homeTeam"))
    away_team = clean_text(raw.get("awayTeam"))
    scheduled_time = clean_text(raw.get("scheduledTime"))
    status_text = clean_text(raw.get("statusText")) or "未开始"
    if not league or not home_team or not away_team or not scheduled_time:
        raise ValueError("详情页缺少联赛、球队或开赛时间")

    home_score, away_score = parse_scores(raw.get("scores"))
    return MatchDetail(
        match_id=int(raw["matchId"]),
        league=league,
        home_team=home_team,
        away_team=away_team,
        scheduled_time=scheduled_time,
        home_score=home_score,
        away_score=away_score,
        status_text=status_text,
    )


async def fetch_detail_async(
    page: AsyncPage,
    match_id: int,
    url_template: str,
    timeout_ms: int,
) -> MatchDetail:
    await page.goto(
        url_template.format(match_id=match_id),
        wait_until="commit",
        timeout=timeout_ms,
    )
    await page.wait_for_selector(
        HEADER_SELECTOR,
        state="attached",
        timeout=timeout_ms,
    )
    raw = await page.evaluate(
        """matchId => ({
            matchId,
            league: document.querySelector('#header .LName')?.innerText,
            homeTeam: document.querySelector('#header .home a')?.innerText,
            awayTeam: document.querySelector('#header .guest a')?.innerText,
            scheduledTime: document.querySelector('#header .time')?.innerText,
            scores: Array.from(
                document.querySelectorAll('#headVs .score')
            ).map(element => element.innerText),
            statusText: document.querySelector('#mState')?.innerText
        })""",
        match_id,
    )
    return parse_detail(raw)


def save_detail(connection: Connection, detail: MatchDetail) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO match_details (
                match_id,
                league,
                home_team,
                away_team,
                scheduled_time,
                home_score,
                away_score,
                status_text
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (match_id) DO UPDATE SET
                league = EXCLUDED.league,
                home_team = EXCLUDED.home_team,
                away_team = EXCLUDED.away_team,
                scheduled_time = EXCLUDED.scheduled_time,
                home_score = EXCLUDED.home_score,
                away_score = EXCLUDED.away_score,
                status_text = EXCLUDED.status_text,
                updated_at = NOW()
            """,
            (
                detail.match_id,
                detail.league,
                detail.home_team,
                detail.away_team,
                detail.scheduled_time,
                detail.home_score,
                detail.away_score,
                detail.status_text,
            ),
        )
    connection.commit()


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


def format_detail(detail: MatchDetail) -> str:
    score = "-"
    if detail.home_score is not None and detail.away_score is not None:
        score = f"{detail.home_score}-{detail.away_score}"
    return (
        f"{detail.match_id} | {detail.league} | {detail.home_team} "
        f"{score} {detail.away_team} | {detail.scheduled_time} | "
        f"{detail.status_text}"
    )


async def fetch_detail_with_retries(
    browser: AsyncBrowser,
    proxy_client: ProxyClient,
    args: argparse.Namespace,
    match_id: int,
) -> MatchDetail:
    """Fetch one detail page with at most two proxy replacements."""

    minimum_lifetime = min(
        args.timeout + 2,
        proxy_client.ttl_seconds - 1,
    )
    for attempt in range(1, MAX_DETAIL_FETCH_ATTEMPTS + 1):
        try:
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
                    return await fetch_detail_async(
                        page,
                        match_id,
                        args.url_template,
                        int(args.timeout * 1000),
                    )
                finally:
                    await context.close()
        except Exception as error:
            print(
                f"{TASK_PREFIX} {match_id} | 第 "
                f"{attempt}/{MAX_DETAIL_FETCH_ATTEMPTS} 次获取失败：{error}",
                file=sys.stderr,
            )
            if attempt == MAX_DETAIL_FETCH_ATTEMPTS:
                raise
            print(
                f"{TASK_PREFIX} {match_id} | 切换代理，开始第 "
                f"{attempt + 1}/{MAX_DETAIL_FETCH_ATTEMPTS} 次尝试。",
                file=sys.stderr,
            )
    raise AssertionError("unreachable")


async def crawl_details(
    connection: Connection,
    match_ids: Sequence[int],
    args: argparse.Namespace,
) -> Tuple[int, int]:
    succeeded = 0
    failed = 0

    proxy_client = ProxyClient.from_env()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        try:
            async def fetch(match_id: int) -> MatchDetail:
                return await fetch_detail_with_retries(
                    browser,
                    proxy_client,
                    args,
                    match_id,
                )

            async for outcome in iter_bounded(
                match_ids,
                args.concurrency,
                fetch,
            ):
                match_id = outcome.job
                if outcome.error is not None:
                    failed += 1
                    connection.rollback()
                    print(
                        f"{TASK_PREFIX} {match_id} | 页面获取失败："
                        f"{outcome.error}",
                        file=sys.stderr,
                    )
                    continue
                detail = outcome.result
                if detail is None:
                    failed += 1
                    print(
                        f"{TASK_PREFIX} {match_id} | 获取失败：没有返回详情",
                        file=sys.stderr,
                    )
                    continue
                try:
                    save_detail(connection, detail)
                except psycopg2.Error as error:
                    failed += 1
                    connection.rollback()
                    print(
                        f"{TASK_PREFIX} {match_id} | 数据库写入失败：{error}",
                        file=sys.stderr,
                    )
                else:
                    succeeded += 1
                    print(f"{TASK_PREFIX} {format_detail(detail)}")
        finally:
            await browser.close()
    return succeeded, failed


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
        return 2

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
        return 1

    if not match_ids:
        connection.close()
        print(f"{TASK_PREFIX} 数据库中没有比赛 ID。", file=sys.stderr)
        return 0

    try:
        succeeded, failed = asyncio.run(
            crawl_details(connection, match_ids, args)
        )
    except Exception as error:
        print(f"{TASK_PREFIX} 详情抓取中断：{error}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    print(
        f"{TASK_PREFIX} 详情抓取完成：成功 {succeeded} 场，"
        f"失败 {failed} 场。",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
