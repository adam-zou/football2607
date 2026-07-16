#!/usr/bin/env python3
"""One-shot completion check for finished matches."""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg2
from dotenv import load_dotenv
from simple_crawler.companies import company_label
from simple_crawler.odds_parser import Titan007OddsParser
from playwright.sync_api import Browser, sync_playwright
from psycopg2.extensions import connection as Connection

try:
    from .fetch_odds_pages import (
        DATABASE_ENV_NAME,
        DEFAULT_BASE_URL,
        ENV_FILE,
        EXIT_MAJORITY_FAILURE,
        EXIT_PARTIAL_FAILURE,
        EXIT_SUCCESS,
        MARKETS,
        block_unneeded_resources,
        build_url,
        env_bool,
        env_company_ids,
        env_float,
        env_optional_int,
        fetch_page_rows,
    )
except ImportError:
    from fetch_odds_pages import (
        DATABASE_ENV_NAME,
        DEFAULT_BASE_URL,
        ENV_FILE,
        EXIT_MAJORITY_FAILURE,
        EXIT_PARTIAL_FAILURE,
        EXIT_SUCCESS,
        MARKETS,
        block_unneeded_resources,
        build_url,
        env_bool,
        env_company_ids,
        env_float,
        env_optional_int,
        fetch_page_rows,
    )

try:
    from .proxy_scheduler import ProxyClient
    from .crawl_status import env_active_crawl_statuses
except ImportError:
    from proxy_scheduler import ProxyClient
    from crawl_status import env_active_crawl_statuses


MARKET_TABLES = {
    "handicap": "titan007_handicap_changes",
    "one_x_two": "titan007_1x2_changes",
    "over_under": "titan007_over_under_changes",
}
DETAIL_SCRIPT = Path(__file__).with_name("fetch_match_details.py")
ODDS_SCRIPT = Path(__file__).with_name("fetch_odds_pages.py")
TASK_PREFIX = "[完成核验]"

ENSURE_MATCH_STATUS_SQL = """
ALTER TABLE match_ids
    ADD COLUMN IF NOT EXISTS crawl_status TEXT NOT NULL DEFAULT '未完成';
ALTER TABLE match_ids
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE match_ids
    DROP CONSTRAINT IF EXISTS match_ids_crawl_status_check;
ALTER TABLE match_ids
    ADD CONSTRAINT match_ids_crawl_status_check
    CHECK (crawl_status IN ('未完成', '已完成', '暂停爬取', '异常'));
"""


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="核验完场比赛的赔率变动记录数并标记抓取完成。"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=env_optional_int(
            parser,
            "SIMPLE_CRAWLER_COMPLETION_MATCH_LIMIT",
        ),
        help="本次最多检查多少场完场比赛",
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
    args.company_ids = env_company_ids(parser)
    args.active_crawl_statuses = env_active_crawl_statuses(parser)

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if "{endpoint}" not in args.base_url:
        parser.error("--base-url 必须包含 {endpoint}")
    return args


def ensure_schema(connection: Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(ENSURE_MATCH_STATUS_SQL)
    connection.commit()


def select_pending_matches(
    connection: Connection,
    limit: Optional[int],
    active_crawl_statuses: Sequence[str],
) -> List[Tuple[int, bool]]:
    statement = """
        SELECT
            ids.match_id,
            details.status_text = '完' AS is_finished
        FROM match_ids AS ids
        JOIN match_details AS details USING (match_id)
        WHERE ids.crawl_status = ANY(%s)
          AND (
              details.status_text = '完'
              OR (
                  details.status_text <> '完'
                  AND
                  details.scheduled_time ~
                      '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                  AND details.scheduled_time::TIMESTAMP
                      AT TIME ZONE 'Asia/Shanghai'
                      <= NOW() - INTERVAL '4 hours'
              )
          )
        ORDER BY
            CASE WHEN details.status_text = '完' THEN 0 ELSE 1 END,
            ids.match_id
    """
    parameters: Tuple[object, ...] = (list(active_crawl_statuses),)
    if limit is not None:
        statement += " LIMIT %s"
        parameters += (limit,)
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return [(int(row[0]), bool(row[1])) for row in cursor.fetchall()]


def load_stored_counts(
    connection: Connection,
    match_id: int,
    company_ids: Sequence[int],
) -> Dict[Tuple[int, str], int]:
    counts: Dict[Tuple[int, str], int] = {}
    with connection.cursor() as cursor:
        for company_id in company_ids:
            for market, table in MARKET_TABLES.items():
                cursor.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {table}
                    WHERE match_id = %s AND company_id = %s
                    """,
                    (match_id, company_id),
                )
                counts[(company_id, market)] = int(cursor.fetchone()[0])
    return counts


def fetch_market_count(
    browser: Browser,
    proxy_scheduler: ProxyClient,
    args: argparse.Namespace,
    match_id: int,
    company_id: int,
    market: str,
) -> int:
    _, selector, _ = MARKETS[market]
    minimum_lifetime = min(
        args.timeout + 2,
        proxy_scheduler.ttl_seconds - 1,
    )
    with proxy_scheduler.lease(
        min_remaining_seconds=minimum_lifetime
    ) as proxy:
        context = browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            proxy=proxy.playwright_options(),
        )
        try:
            page = context.new_page()
            page.route("**/*", block_unneeded_resources)
            rows = fetch_page_rows(
                page,
                build_url(args.base_url, match_id, company_id, market),
                selector,
                args.timeout,
            )
            changes = Titan007OddsParser.parse_rows(
                market,
                rows,
                match_id=match_id,
                company_id=company_id,
            )
            return len(changes)
        finally:
            context.close()


def check_match(
    browser: Browser,
    proxy_scheduler: ProxyClient,
    connection: Connection,
    args: argparse.Namespace,
    match_id: int,
) -> Tuple[bool, str]:
    stored_counts = load_stored_counts(
        connection,
        match_id,
        args.company_ids,
    )
    for company_id in args.company_ids:
        for market, (_, _, label) in MARKETS.items():
            try:
                fetched_count = fetch_market_count(
                    browser,
                    proxy_scheduler,
                    args,
                    match_id,
                    company_id,
                    market,
                )
            except Exception as error:
                return (
                    False,
                    f"{company_label(company_id)} {label}获取失败：{error}",
                )
            stored_count = stored_counts[(company_id, market)]
            if fetched_count != stored_count:
                return (
                    False,
                    f"{company_label(company_id)} {label}赔率变动记录数不一致："
                    f"页面 {fetched_count}，数据库 {stored_count}",
                )
    return True, "全部公司和市场的赔率变动记录数一致"


def mark_completed(connection: Connection, match_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE match_ids
            SET crawl_status = '已完成', updated_at = NOW()
            WHERE match_id = %s AND crawl_status <> '已完成'
            """,
            (match_id,),
        )
    connection.commit()


def refresh_detail_once(match_id: int) -> int:
    result = subprocess.run(
        [sys.executable, str(DETAIL_SCRIPT), str(match_id)],
        check=False,
    )
    return result.returncode


def is_match_finished(connection: Connection, match_id: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT status_text = '完'
            FROM match_details
            WHERE match_id = %s
            """,
            (match_id,),
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def refresh_odds_once(match_id: int) -> int:
    result = subprocess.run(
        [sys.executable, str(ODDS_SCRIPT), str(match_id)],
        check=False,
    )
    return result.returncode


def final_status_for_refresh(returncode: int) -> Optional[str]:
    if returncode in (EXIT_SUCCESS, EXIT_PARTIAL_FAILURE):
        return "暂停爬取"
    if returncode == EXIT_MAJORITY_FAILURE:
        return "异常"
    return None


def mark_final_status(
    connection: Connection,
    match_id: int,
    crawl_status: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE match_ids
            SET crawl_status = %s, updated_at = NOW()
            WHERE match_id = %s
            """,
            (crawl_status, match_id),
        )
    connection.commit()


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
        connection = psycopg2.connect(database_url)
        ensure_schema(connection)
        pending_matches = select_pending_matches(
            connection,
            args.limit,
            args.active_crawl_statuses,
        )
    except psycopg2.Error as error:
        print(f"{TASK_PREFIX} 数据库访问失败：{error}", file=sys.stderr)
        return 1

    if not pending_matches:
        connection.close()
        print(
            f"{TASK_PREFIX} 没有需要核验或暂停的比赛。",
            file=sys.stderr,
        )
        return 0

    completed = 0
    incomplete = 0
    paused = 0
    abnormal = 0
    try:
        with ProxyClient.from_env() as proxy_scheduler:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not args.headed)
                try:
                    for match_id, is_finished in pending_matches:
                        if not is_finished:
                            detail_returncode = refresh_detail_once(match_id)
                            if detail_returncode != 0:
                                incomplete += 1
                                print(
                                    f"{TASK_PREFIX} {match_id} | 未更新 | "
                                    "最后一次详情刷新失败，保留原状态"
                                )
                                continue
                            if is_match_finished(connection, match_id):
                                incomplete += 1
                                print(
                                    f"{TASK_PREFIX} {match_id} | 未完成 | "
                                    "详情已更新为完场，留待下一轮核验赔率完整性"
                                )
                                continue
                            returncode = refresh_odds_once(match_id)
                            final_status = final_status_for_refresh(returncode)
                            if final_status is None:
                                incomplete += 1
                                print(
                                    f"{TASK_PREFIX} {match_id} | 未更新 | "
                                    "最后一次赔率抓取整体中断，保留原状态"
                                )
                                continue
                            mark_final_status(
                                connection,
                                match_id,
                                final_status,
                            )
                            if final_status == "暂停爬取":
                                paused += 1
                            else:
                                abnormal += 1
                            refresh_message = (
                                "最后一次赔率抓取成功"
                                if returncode == EXIT_SUCCESS
                                else (
                                    "存在失败页面，但未超过一半"
                                    if returncode == EXIT_PARTIAL_FAILURE
                                    else "超过一半页面失败"
                                )
                            )
                            print(
                                f"{TASK_PREFIX} {match_id} | {final_status} | "
                                f"开赛时间已超过 4 小时；{refresh_message}"
                            )
                            continue
                        matched, message = check_match(
                            browser,
                            proxy_scheduler,
                            connection,
                            args,
                            match_id,
                        )
                        if matched:
                            mark_completed(connection, match_id)
                            completed += 1
                            print(
                                f"{TASK_PREFIX} {match_id} | 已完成 | {message}"
                            )
                        else:
                            incomplete += 1
                            print(
                                f"{TASK_PREFIX} {match_id} | 未完成 | {message}"
                            )
                finally:
                    browser.close()
    except Exception as error:
        print(f"{TASK_PREFIX} 完成核验中断：{error}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    print(
        f"{TASK_PREFIX} 完成核验结束：标记完成 {completed} 场，"
        f"暂停爬取 {paused} 场，"
        f"异常 {abnormal} 场，"
        f"仍未完成 {incomplete} 场。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
