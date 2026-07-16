#!/usr/bin/env python3
"""Fetch the match IDs currently shown on the Titan007 match list."""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import psycopg2
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from psycopg2.extras import execute_values

try:
    from .proxy_scheduler import ProxyClient
except ImportError:
    from proxy_scheduler import ProxyClient


DEFAULT_URL = "https://live.titan007.com/oldIndexall.aspx"
ROW_SELECTOR = 'tr[id^="tr1_"]'
ROW_ID_PATTERN = re.compile(r"tr1_(\d+)")
ENV_FILE = Path(__file__).with_name(".env")
DATABASE_ENV_NAME = "SIMPLE_CRAWLER_DATABASE_URL"
TASK_PREFIX = "[比赛 ID]"
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
INSERT_MATCH_IDS_SQL = """
INSERT INTO match_ids (match_id)
VALUES %s
ON CONFLICT (match_id) DO NOTHING
RETURNING match_id
"""


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="获取 Titan007 当前比赛列表中的比赛 ID。"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("SIMPLE_CRAWLER_LIST_URL", DEFAULT_URL),
        help="比赛列表页地址",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=env_float(parser, "SIMPLE_CRAWLER_LIST_TIMEOUT_SECONDS", 15.0),
        help="等待列表出现的秒数（默认：15）",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=env_float(parser, "SIMPLE_CRAWLER_LIST_SETTLE_SECONDS", 1.0),
        help="列表出现后继续等待的秒数（默认：1）",
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

    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.settle < 0:
        parser.error("--settle 不能小于 0")
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


def extract_match_ids(page: Page, url: str, timeout_ms: int, settle_ms: int) -> List[int]:
    page.goto(url, wait_until="commit", timeout=timeout_ms)
    page.wait_for_selector(ROW_SELECTOR, state="attached", timeout=timeout_ms)
    if settle_ms:
        page.wait_for_timeout(settle_ms)

    row_ids = page.locator(ROW_SELECTOR).evaluate_all(
        "rows => rows.map(row => row.id)"
    )
    match_ids = {
        int(matched.group(1))
        for row_id in row_ids
        if (matched := ROW_ID_PATTERN.fullmatch(str(row_id))) is not None
        and int(matched.group(1)) > 0
    }
    return sorted(match_ids)


def save_match_ids(database_url: str, match_ids: Sequence[int]) -> int:
    """Create the table and insert IDs that have not been seen before."""

    connection = psycopg2.connect(database_url)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(CREATE_MATCH_IDS_TABLE_SQL)
                if not match_ids:
                    return 0
                inserted_rows = execute_values(
                    cursor,
                    INSERT_MATCH_IDS_SQL,
                    [(match_id,) for match_id in match_ids],
                    fetch=True,
                )
                return len(inserted_rows)
    finally:
        connection.close()


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

    timeout_ms = int(args.timeout * 1000)
    settle_ms = int(args.settle * 1000)

    try:
        proxy_client = ProxyClient.from_env()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            try:
                minimum_lifetime = min(
                    args.timeout + args.settle + 2,
                    proxy_client.ttl_seconds - 1,
                )
                with proxy_client.lease(
                    min_remaining_seconds=minimum_lifetime
                ) as proxy:
                    context = browser.new_context(
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        proxy=proxy.playwright_options(),
                    )
                    try:
                        page = context.new_page()
                        match_ids = extract_match_ids(
                            page,
                            args.url,
                            timeout_ms,
                            settle_ms,
                        )
                    finally:
                        context.close()
            finally:
                browser.close()
    except PlaywrightTimeoutError:
        print(
            f"{TASK_PREFIX} 错误：{args.timeout:g} 秒内没有获取到比赛列表。",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(f"{TASK_PREFIX} 错误：{error}", file=sys.stderr)
        return 1

    try:
        inserted_count = save_match_ids(database_url, match_ids)
    except psycopg2.Error as error:
        print(f"{TASK_PREFIX} 数据库写入失败：{error}", file=sys.stderr)
        return 1

    for match_id in match_ids:
        print(f"{TASK_PREFIX} {match_id}")
    print(
        f"{TASK_PREFIX} 共获取 {len(match_ids)} 个比赛 ID，"
        f"数据库新增 {inserted_count} 个。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
