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
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from psycopg2.extras import execute_values

try:
    from .proxy_scheduler import ProxyClient
except ImportError:
    from proxy_scheduler import ProxyClient


DEFAULT_URL = "https://live.titan007.com/oldIndexall.aspx"
MATCH_DATA_URL_MARKER = "/vbsxml/bfdata_ut.js"
MATCH_DATA_ID_PATTERN = re.compile(r'A\[\d+\]\s*=\s*"(\d+)\^')
MATCH_DATA_COUNT_PATTERN = re.compile(r"\bmatchcount\s*=\s*(\d+)\s*;")
BLOCKED_RESOURCE_TYPES = {"stylesheet", "image", "media", "font"}
ENV_FILE = Path(__file__).with_name(".env")
DATABASE_ENV_NAME = "SIMPLE_CRAWLER_DATABASE_URL"
TASK_PREFIX = "[比赛 ID]"
MAX_FETCH_ATTEMPTS = 3
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
        help="兼容参数；响应解析模式不再额外等待（默认：1）",
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


def route_list_resource(route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def extract_match_ids_from_bfdata(source: str) -> List[int]:
    count_match = MATCH_DATA_COUNT_PATTERN.search(source)
    if count_match is None:
        raise RuntimeError("比赛数据响应缺少 matchcount")
    declared_count = int(count_match.group(1))
    match_ids = sorted(
        {
            int(value)
            for value in MATCH_DATA_ID_PATTERN.findall(source)
            if int(value) > 0
        }
    )
    if len(match_ids) != declared_count:
        raise RuntimeError(
            "比赛数据响应声明数量与解析 ID 数量不一致："
            f"声明 {declared_count}，解析 {len(match_ids)}"
        )
    return match_ids


def extract_match_ids(
    page: Page,
    url: str,
    timeout_ms: int,
    settle_ms: int,
) -> List[int]:
    """Read the complete match set from bfdata without querying the DOM."""

    del settle_ms  # Retained in the public CLI for configuration compatibility.
    page.route("**/*", route_list_resource)
    with page.expect_response(
        lambda response: MATCH_DATA_URL_MARKER in response.url,
        timeout=timeout_ms,
    ) as response_info:
        navigation_response = page.goto(
            url,
            wait_until="commit",
            timeout=timeout_ms,
        )
    if navigation_response is None or navigation_response.status >= 400:
        status = (
            "无响应"
            if navigation_response is None
            else navigation_response.status
        )
        raise RuntimeError(f"比赛列表页返回 HTTP {status}")
    data_response = response_info.value
    if data_response.status >= 400:
        raise RuntimeError(f"比赛数据响应返回 HTTP {data_response.status}")
    return extract_match_ids_from_bfdata(data_response.text())


def fetch_match_ids_with_retries(
    browser: Browser,
    proxy_client: ProxyClient,
    args: argparse.Namespace,
) -> List[int]:
    """Fetch the list with at most two same-round proxy replacements."""

    timeout_ms = int(args.timeout * 1000)
    settle_ms = int(args.settle * 1000)
    minimum_lifetime = min(
        args.timeout + 2,
        proxy_client.ttl_seconds - 1,
    )

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            with proxy_client.lease(
                min_remaining_seconds=minimum_lifetime
            ) as proxy:
                context = browser.new_context(
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    proxy=proxy.playwright_options(),
                )
                try:
                    return extract_match_ids(
                        context.new_page(),
                        args.url,
                        timeout_ms,
                        settle_ms,
                    )
                finally:
                    context.close()
        except Exception as error:
            print(
                f"{TASK_PREFIX} 第 {attempt}/{MAX_FETCH_ATTEMPTS} 次"
                f"获取失败：{error}",
                file=sys.stderr,
            )
            if attempt == MAX_FETCH_ATTEMPTS:
                raise
            print(
                f"{TASK_PREFIX} 切换代理，开始第 "
                f"{attempt + 1}/{MAX_FETCH_ATTEMPTS} 次尝试。",
                file=sys.stderr,
            )
    raise AssertionError("unreachable")


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

    try:
        proxy_client = ProxyClient.from_env()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            try:
                match_ids = fetch_match_ids_with_retries(
                    browser,
                    proxy_client,
                    args,
                )
            finally:
                browser.close()
    except PlaywrightTimeoutError:
        print(
            f"{TASK_PREFIX} 连续 {MAX_FETCH_ATTEMPTS} 次失败："
            f"最后一次在 {args.timeout:g} 秒内没有获取到比赛列表。",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(
            f"{TASK_PREFIX} 连续 {MAX_FETCH_ATTEMPTS} 次失败：{error}",
            file=sys.stderr,
        )
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
