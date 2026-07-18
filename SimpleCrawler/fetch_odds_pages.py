#!/usr/bin/env python3
"""Fetch and store three Titan007 odds markets per company."""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import psycopg2
from dotenv import load_dotenv
from simple_crawler.companies import COMPANY_IDS, company_label
from simple_crawler.monitoring import format_round_match_count
from playwright.async_api import async_playwright
from psycopg2.extensions import connection as Connection

try:
    from .concurrent_pages import iter_bounded
    from .proxy_scheduler import ProxyClient
    from .crawl_status import env_active_crawl_statuses
    from .odds_collection import (
        MARKETS,
        MarketCollectionOutcome,
        OddsCompanyJob,
        OddsCollectionConfig,
        OddsPageJob,
        collect_company_markets_async,
        ensure_odds_schema,
        persist_market_failure,
        persist_market_page,
    )
except ImportError:
    from concurrent_pages import iter_bounded
    from proxy_scheduler import ProxyClient
    from crawl_status import env_active_crawl_statuses
    from odds_collection import (
        MARKETS,
        MarketCollectionOutcome,
        OddsCompanyJob,
        OddsCollectionConfig,
        OddsPageJob,
        collect_company_markets_async,
        ensure_odds_schema,
        persist_market_failure,
        persist_market_page,
    )


ENV_FILE = Path(__file__).with_name(".env")
DATABASE_ENV_NAME = "SIMPLE_CRAWLER_DATABASE_URL"
DEFAULT_BASE_URL = "https://vip.titan007.com/changeDetail/{endpoint}"
DEFAULT_COMPANY_IDS = list(COMPANY_IDS)
SUPPORTED_COMPANY_IDS = set(DEFAULT_COMPANY_IDS)
TASK_PREFIX = "[赔率]"
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_INDETERMINATE = 3
EXIT_MAJORITY_FAILURE = 10

CREATE_MATCH_IDS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS match_ids (
    match_id BIGINT PRIMARY KEY,
    crawl_status TEXT NOT NULL DEFAULT '未完成'
        CHECK (crawl_status IN ('未完成', '已完成', '暂停爬取', '异常')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


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
        help="赔率页面导航的超时秒数",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=env_int(
            parser,
            "SIMPLE_CRAWLER_ODDS_PAGE_CONCURRENCY",
            12,
        ),
        help="同时抓取的比赛×公司任务数量（默认：12）",
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
        ensure_odds_schema(cursor)
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
                SELECT ids.match_id
                FROM match_ids AS ids
                JOIN match_details AS details USING (match_id)
                WHERE ids.match_id = ANY(%s)
                  AND ids.crawl_status = ANY(%s)
                  AND details.scheduled_time ~
                      '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                  AND details.scheduled_time::TIMESTAMP
                      AT TIME ZONE 'Asia/Shanghai'
                      >= NOW() - INTERVAL '4 hours'
                  AND details.scheduled_time::TIMESTAMP
                      AT TIME ZONE 'Asia/Shanghai'
                      <= NOW() + INTERVAL '30 minutes'
                  AND (
                      details.status_text <> '完'
                      OR details.updated_at > NOW() - INTERVAL '5 minutes'
                  )
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
            JOIN match_details AS details USING (match_id)
            WHERE ids.crawl_status = ANY(%s)
              AND details.scheduled_time ~
                  '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
              AND details.scheduled_time::TIMESTAMP
                  AT TIME ZONE 'Asia/Shanghai'
                  >= NOW() - INTERVAL '4 hours'
              AND details.scheduled_time::TIMESTAMP
                  AT TIME ZONE 'Asia/Shanghai'
                  <= NOW() + INTERVAL '30 minutes'
              AND (
                  details.status_text <> '完'
                  OR details.updated_at > NOW() - INTERVAL '5 minutes'
              )
            ORDER BY
                CASE
                    WHEN details.status_text <> '完'
                         AND details.scheduled_time::TIMESTAMP
                             AT TIME ZONE 'Asia/Shanghai' <= NOW()
                    THEN 0
                    WHEN details.scheduled_time::TIMESTAMP
                        AT TIME ZONE 'Asia/Shanghai' > NOW()
                    THEN 1
                    WHEN details.status_text = '完' THEN 2
                    ELSE 3
                END,
                CASE
                    WHEN details.scheduled_time::TIMESTAMP
                        AT TIME ZONE 'Asia/Shanghai' > NOW()
                    THEN details.scheduled_time::TIMESTAMP
                        AT TIME ZONE 'Asia/Shanghai'
                END ASC NULLS LAST,
                details.scheduled_time::TIMESTAMP
                    AT TIME ZONE 'Asia/Shanghai' DESC,
                ids.match_id
        """
        parameters: Tuple[object, ...] = (list(active_crawl_statuses),)
        if limit is not None:
            statement += " LIMIT %s"
            parameters += (limit,)
        cursor.execute(statement, parameters)
        return [row[0] for row in cursor.fetchall()]


def record_market_failure_or_log(
    connection: Connection,
    job: OddsPageJob,
    error: str,
) -> None:
    try:
        persist_market_failure(connection, job, error)
    except Exception as state_error:
        connection.rollback()
        print(
            f"{TASK_PREFIX} {job.match_id} | "
            f"{company_label(job.company_id)} | "
            f"{MARKETS[job.market][2]}状态写入失败：{state_error}",
            file=sys.stderr,
        )


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
        OddsCompanyJob(match_id, company_id, tuple(MARKETS))
        for match_id in match_ids
        for company_id in args.company_ids
    ]
    proxy_client = ProxyClient.from_env()
    collection_config = OddsCollectionConfig(
        base_url=args.base_url,
        timeout_seconds=args.timeout,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        try:
            async def fetch(
                job: OddsCompanyJob,
            ) -> List[MarketCollectionOutcome]:
                return await collect_company_markets_async(
                    browser,
                    proxy_client,
                    collection_config,
                    job,
                )

            async for outcome in iter_bounded(
                jobs,
                args.concurrency,
                fetch,
            ):
                if outcome.error is not None:
                    market_outcomes = [
                        MarketCollectionOutcome(
                            job=page_job,
                            error=outcome.error,
                        )
                        for page_job in outcome.job.page_jobs()
                    ]
                else:
                    market_outcomes = outcome.result or []
                for market_outcome in market_outcomes:
                    job = market_outcome.job
                    label = MARKETS[job.market][2]
                    if market_outcome.error is not None:
                        failed += 1
                        connection.rollback()
                        record_market_failure_or_log(
                            connection,
                            job,
                            str(market_outcome.error),
                        )
                        print(
                            f"{TASK_PREFIX} {job.match_id} | "
                            f"{company_label(job.company_id)} | "
                            f"{label}失败：{market_outcome.error}",
                            file=sys.stderr,
                        )
                        continue
                    changes = market_outcome.changes
                    if changes is None:
                        failed += 1
                        record_market_failure_or_log(
                            connection,
                            job,
                            "没有返回解析结果",
                        )
                        continue
                    try:
                        persist_market_page(connection, job, changes)
                    except Exception as error:
                        failed += 1
                        connection.rollback()
                        record_market_failure_or_log(
                            connection,
                            job,
                            f"数据库写入失败：{error}",
                        )
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

    print(format_round_match_count(TASK_PREFIX, len(match_ids)))
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
