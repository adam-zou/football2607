#!/usr/bin/env python3
"""Collect and persist final odds snapshots for overdue matches."""

import argparse
import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import psycopg2
from dotenv import load_dotenv
from playwright.async_api import Browser as AsyncBrowser
from playwright.async_api import async_playwright
from psycopg2.extensions import connection as Connection
from simple_crawler.companies import COMPANY_IDS, company_label

try:
    from .concurrent_pages import iter_bounded
    from .crawl_status import env_active_crawl_statuses
    from .fetch_odds_pages import (
        DATABASE_ENV_NAME,
        DEFAULT_BASE_URL,
        ENV_FILE,
        env_bool,
        env_float,
        env_int,
        env_optional_int,
    )
    from .odds_collection import (
        MARKETS,
        MarketCollectionOutcome,
        OddsCollectionConfig,
        OddsPageJob,
        collect_company_markets_async,
        ensure_odds_schema,
        group_page_jobs_by_company,
        persist_market_failure,
        persist_market_page,
    )
    from .odds_market_state import (
        final_snapshot_complete,
        load_pending_final_pages,
        prepare_final_snapshot,
    )
    from .proxy_scheduler import ProxyClient
except ImportError:
    from concurrent_pages import iter_bounded
    from crawl_status import env_active_crawl_statuses
    from fetch_odds_pages import (
        DATABASE_ENV_NAME,
        DEFAULT_BASE_URL,
        ENV_FILE,
        env_bool,
        env_float,
        env_int,
        env_optional_int,
    )
    from odds_collection import (
        MARKETS,
        MarketCollectionOutcome,
        OddsCollectionConfig,
        OddsPageJob,
        collect_company_markets_async,
        ensure_odds_schema,
        group_page_jobs_by_company,
        persist_market_failure,
        persist_market_page,
    )
    from odds_market_state import (
        final_snapshot_complete,
        load_pending_final_pages,
        prepare_final_snapshot,
    )
    from proxy_scheduler import ProxyClient


DETAIL_SCRIPT = Path(__file__).with_name("fetch_match_details.py")
TASK_PREFIX = "[完成核验]"
MAX_MARKET_FETCH_ATTEMPTS = 3
DEFAULT_MATCH_TIMEOUT_SECONDS = 180.0
FINAL_DETAIL_CRAWL_STATUSES = "未完成,暂停爬取,异常"

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


@dataclass(frozen=True)
class FinalizationResult:
    match_id: int
    succeeded: int
    failed: int
    pending: int
    completed: bool
    timed_out: bool = False


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="为开赛超过 4 小时的比赛采集并写入最终赔率快照。"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=env_optional_int(
            parser,
            "SIMPLE_CRAWLER_COMPLETION_MATCH_LIMIT",
        ),
        help="本次最多收尾多少场比赛",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=env_float(
            parser,
            "SIMPLE_CRAWLER_ODDS_TIMEOUT_SECONDS",
            15.0,
        ),
        help="每个赔率页面请求的超时秒数",
    )
    parser.add_argument(
        "--match-timeout",
        type=float,
        default=env_float(
            parser,
            "SIMPLE_CRAWLER_COMPLETION_MATCH_TIMEOUT_SECONDS",
            DEFAULT_MATCH_TIMEOUT_SECONDS,
        ),
        help="单场最终快照的总超时秒数（默认：180）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=env_int(
            parser,
            "SIMPLE_CRAWLER_ODDS_PAGE_CONCURRENCY",
            12,
        ),
        help="同时采集的最终比赛×公司任务数（默认：12）",
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
    parser.set_defaults(headed=env_bool(parser, "SIMPLE_CRAWLER_HEADED", False))
    args = parser.parse_args(argv)
    args.company_ids = list(COMPANY_IDS)
    args.active_crawl_statuses = env_active_crawl_statuses(parser)

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.match_timeout <= 0:
        parser.error("--match-timeout 必须大于 0")
    if args.concurrency <= 0:
        parser.error("--concurrency 必须大于 0")
    if "{endpoint}" not in args.base_url:
        parser.error("--base-url 必须包含 {endpoint}")
    return args


def ensure_schema(connection: Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(ENSURE_MATCH_STATUS_SQL)
        ensure_odds_schema(cursor)
    connection.commit()


def select_finalization_matches(
    connection: Connection,
    limit: Optional[int],
    active_crawl_statuses: Sequence[str],
) -> List[Tuple[int, bool, str]]:
    statement = """
        SELECT
            ids.match_id,
            details.status_text = '完' AS is_finished,
            ids.crawl_status
        FROM match_ids AS ids
        JOIN match_details AS details USING (match_id)
        WHERE ids.crawl_status <> '已完成'
          AND (
              ids.crawl_status = ANY(%s)
              OR ids.crawl_status IN ('暂停爬取', '异常')
          )
          AND (
              (
                  details.status_text = '完'
                  AND details.updated_at <= NOW() - INTERVAL '5 minutes'
              )
              OR (
                  details.scheduled_time ~
                      '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                  AND details.scheduled_time::TIMESTAMP
                      AT TIME ZONE 'Asia/Shanghai'
                      < NOW() - INTERVAL '4 hours'
              )
          )
        ORDER BY
            (details.status_text = '完') DESC,
            CASE
                WHEN details.scheduled_time ~
                    '^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$'
                THEN details.scheduled_time::TIMESTAMP
                    AT TIME ZONE 'Asia/Shanghai'
            END DESC NULLS LAST,
            ids.match_id
    """
    parameters: Tuple[object, ...] = (list(active_crawl_statuses),)
    if limit is not None:
        statement += " LIMIT %s"
        parameters += (limit,)
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return [
            (int(row[0]), bool(row[1]), str(row[2]))
            for row in cursor.fetchall()
        ]


def refresh_detail_once(match_id: int) -> int:
    environment = os.environ.copy()
    environment["SIMPLE_CRAWLER_ACTIVE_CRAWL_STATUSES"] = (
        FINAL_DETAIL_CRAWL_STATUSES
    )
    result = subprocess.run(
        [sys.executable, str(DETAIL_SCRIPT), str(match_id)],
        check=False,
        env=environment,
    )
    return result.returncode


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


def prepare_pending_jobs(
    connection: Connection,
    match_id: int,
    company_ids: Sequence[int],
) -> List[OddsPageJob]:
    with connection.cursor() as cursor:
        prepare_final_snapshot(
            cursor,
            match_id=match_id,
            company_ids=company_ids,
            markets=list(MARKETS),
        )
        pending = load_pending_final_pages(cursor, match_id, company_ids)
    connection.commit()
    return [
        OddsPageJob(match_id, company_id, market)
        for company_id, market in pending
    ]


def load_pending_jobs(
    connection: Connection,
    match_id: int,
    company_ids: Sequence[int],
) -> List[OddsPageJob]:
    with connection.cursor() as cursor:
        pending = load_pending_final_pages(cursor, match_id, company_ids)
    return [
        OddsPageJob(match_id, company_id, market)
        for company_id, market in pending
    ]


def is_final_snapshot_complete(
    connection: Connection,
    match_id: int,
    company_ids: Sequence[int],
) -> bool:
    with connection.cursor() as cursor:
        return final_snapshot_complete(
            cursor,
            match_id,
            company_ids,
            len(MARKETS),
        )


def persist_final_failure_or_log(
    connection: Connection,
    job: OddsPageJob,
    error: str,
) -> None:
    try:
        persist_market_failure(connection, job, error, final=True)
    except Exception as state_error:
        connection.rollback()
        print(
            f"{TASK_PREFIX} {job.match_id} | "
            f"{company_label(job.company_id)} | "
            f"{MARKETS[job.market][2]}最终状态写入失败：{state_error}",
            file=sys.stderr,
        )


async def run_final_jobs(
    browser: AsyncBrowser,
    proxy_client: ProxyClient,
    connection: Connection,
    args: argparse.Namespace,
    jobs: Sequence[OddsPageJob],
) -> Tuple[int, int]:
    config = OddsCollectionConfig(
        base_url=args.base_url,
        timeout_seconds=args.timeout,
    )
    succeeded = 0
    pending = list(jobs)

    for attempt in range(1, MAX_MARKET_FETCH_ATTEMPTS + 1):
        if not pending:
            break
        company_jobs = group_page_jobs_by_company(pending)
        next_pending: List[OddsPageJob] = []

        async def fetch(company_job):
            return await collect_company_markets_async(
                browser,
                proxy_client,
                config,
                company_job,
            )

        async for outcome in iter_bounded(
            company_jobs,
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
                error = market_outcome.error
                if error is None and market_outcome.changes is None:
                    error = RuntimeError("没有返回解析结果")
                if error is None:
                    try:
                        persist_market_page(
                            connection,
                            job,
                            market_outcome.changes or [],
                            final=True,
                        )
                    except Exception as persist_error:
                        connection.rollback()
                        error = RuntimeError(
                            f"数据库写入失败：{persist_error}"
                        )
                if error is not None:
                    connection.rollback()
                    persist_final_failure_or_log(
                        connection,
                        job,
                        str(error),
                    )
                    next_pending.append(job)
                    print(
                        f"{TASK_PREFIX} {job.match_id} | "
                        f"{company_label(job.company_id)} | {label} | "
                        f"第 {attempt}/{MAX_MARKET_FETCH_ATTEMPTS} "
                        f"次失败：{error}",
                        file=sys.stderr,
                    )
                else:
                    succeeded += 1
                    print(
                        f"{TASK_PREFIX} {job.match_id} | "
                        f"{company_label(job.company_id)} | {label} | "
                        f"最终快照 {len(market_outcome.changes or [])} 条"
                    )
        pending = next_pending
        if pending and attempt < MAX_MARKET_FETCH_ATTEMPTS:
            print(
                f"{TASK_PREFIX} 仅重试失败市场 {len(pending)} 页，"
                f"开始第 {attempt + 1}/{MAX_MARKET_FETCH_ATTEMPTS} 次尝试。",
                file=sys.stderr,
            )
    return succeeded, len(pending)


async def finalize_match(
    browser: AsyncBrowser,
    proxy_client: ProxyClient,
    connection: Connection,
    args: argparse.Namespace,
    match_id: int,
) -> FinalizationResult:
    jobs = prepare_pending_jobs(connection, match_id, args.company_ids)
    succeeded = 0
    failed = 0
    timed_out = False
    if jobs:
        try:
            succeeded, failed = await asyncio.wait_for(
                run_final_jobs(
                    browser,
                    proxy_client,
                    connection,
                    args,
                    jobs,
                ),
                timeout=args.match_timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True

    completed = is_final_snapshot_complete(
        connection,
        match_id,
        args.company_ids,
    )
    if completed:
        mark_completed(connection, match_id)
    remaining = len(load_pending_jobs(connection, match_id, args.company_ids))
    return FinalizationResult(
        match_id=match_id,
        succeeded=succeeded,
        failed=failed,
        pending=remaining,
        completed=completed,
        timed_out=timed_out,
    )


async def finalize_matches(
    connection: Connection,
    args: argparse.Namespace,
    matches: Sequence[Tuple[int, bool, str]],
) -> List[FinalizationResult]:
    results: List[FinalizationResult] = []
    proxy_client = ProxyClient.from_env()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        try:
            for match_id, is_finished, _crawl_status in matches:
                if not is_finished:
                    detail_returncode = refresh_detail_once(match_id)
                    if detail_returncode != 0:
                        results.append(
                            FinalizationResult(
                                match_id=match_id,
                                succeeded=0,
                                failed=0,
                                pending=len(args.company_ids) * len(MARKETS),
                                completed=False,
                            )
                        )
                        print(
                            f"{TASK_PREFIX} {match_id} | 未完成 | "
                            "最终详情刷新失败，留待下一轮",
                            file=sys.stderr,
                        )
                        continue
                result = await finalize_match(
                    browser,
                    proxy_client,
                    connection,
                    args,
                    match_id,
                )
                results.append(result)
                if result.completed:
                    print(
                        f"{TASK_PREFIX} {match_id} | 已完成 | "
                        "全部最终快照页面已成功写入"
                    )
                else:
                    timeout_text = "；单场超时" if result.timed_out else ""
                    print(
                        f"{TASK_PREFIX} {match_id} | 未完成 | "
                        f"本轮成功 {result.succeeded}，"
                        f"失败 {result.failed}，"
                        f"待处理 {result.pending}{timeout_text}"
                    )
        finally:
            await browser.close()
    return results


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
        matches = select_finalization_matches(
            connection,
            args.limit,
            args.active_crawl_statuses,
        )
    except psycopg2.Error as error:
        if connection is not None:
            connection.close()
        print(f"{TASK_PREFIX} 数据库访问失败：{error}", file=sys.stderr)
        return 1

    if not matches:
        connection.close()
        print(f"{TASK_PREFIX} 没有需要收尾的比赛。", file=sys.stderr)
        return 0

    try:
        results = asyncio.run(finalize_matches(connection, args, matches))
    except Exception as error:
        print(f"{TASK_PREFIX} 最终快照中断：{error}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    completed = sum(result.completed for result in results)
    incomplete = len(results) - completed
    print(
        f"{TASK_PREFIX} 最终快照结束：完成 {completed} 场，"
        f"待续跑 {incomplete} 场。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
