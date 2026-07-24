#!/usr/bin/env python3
"""Backfill Titan007 archive match IDs into PostgreSQL."""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Set
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv
from lxml import html as lxml_html
from psycopg2.extras import execute_values


URL_TEMPLATE = "https://bf.titan007.com/football/Over_{date}.htm"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_BATCH_SIZE = 20
DEFAULT_INTERVAL_SECONDS = 300.0
DATABASE_ENV_NAME = "SIMPLE_CRAWLER_DATABASE_URL"
BATCH_SIZE_ENV_NAME = "SIMPLE_CRAWLER_ARCHIVE_BATCH_SIZE"
INTERVAL_ENV_NAME = "SIMPLE_CRAWLER_ARCHIVE_INTERVAL_SECONDS"
ENV_FILE = Path(__file__).with_name(".env")
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
TASK_PREFIX = "[归档比赛 ID]"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/138.0"

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


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"日期必须使用 YYYYMMDD 格式：{value}"
        ) from error


def parse_args(
    argv: Optional[Sequence[str]] = None,
    today: Optional[date] = None,
) -> argparse.Namespace:
    current_date = today or datetime.now(SHANGHAI_TIMEZONE).date()
    parser = argparse.ArgumentParser(
        description="按日期倒序回补 Titan007 完场归档比赛 ID。"
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=current_date.replace(month=1, day=1),
        help="最早日期，格式 YYYYMMDD（默认：当年 1 月 1 日）",
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=current_date,
        help="最晚日期，格式 YYYYMMDD（默认：今天）",
    )
    args = parser.parse_args(argv)
    if args.start_date > args.end_date:
        parser.error("--start-date 不能晚于 --end-date")
    return args


def env_number(name: str, default: float, integer: bool = False):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return int(default) if integer else default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError as error:
        raise ValueError(f"{name} 必须是数字") from error
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def iter_dates_descending(start_date: date, end_date: date) -> Iterable[date]:
    current = end_date
    while current >= start_date:
        yield current
        current -= timedelta(days=1)


def extract_match_ids_from_html(source: bytes) -> List[int]:
    document = lxml_html.fromstring(source)
    tables = document.xpath('//*[@id="table_live"]')
    if not tables:
        raise RuntimeError("页面缺少比赛列表 table_live")

    match_ids: List[int] = []
    seen = set()
    for raw_id in tables[0].xpath('.//tr[@sid]/@sid'):
        value = raw_id.strip()
        if not value.isdigit() or int(value) <= 0:
            raise RuntimeError(f"比赛行包含无效 sId：{raw_id!r}")
        match_id = int(value)
        if match_id not in seen:
            seen.add(match_id)
            match_ids.append(match_id)
    return match_ids


def fetch_archive_match_ids(
    archive_date: date,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    open_url: Callable = urlopen,
) -> List[int]:
    url = URL_TEMPLATE.format(date=archive_date.strftime("%Y%m%d"))
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with open_url(request, timeout=timeout) as response:
            source = response.read()
    except HTTPError as error:
        raise RuntimeError(f"{archive_date:%Y%m%d} 返回 HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(
            f"{archive_date:%Y%m%d} 无法访问：{error.reason}"
        ) from error
    return extract_match_ids_from_html(source)


def collect_match_ids(
    start_date: date,
    end_date: date,
    fetcher: Callable[[date], List[int]] = fetch_archive_match_ids,
) -> List[int]:
    match_ids: List[int] = []
    seen = set()
    for archive_date in iter_dates_descending(start_date, end_date):
        daily_ids = fetcher(archive_date)
        print(
            f"{TASK_PREFIX} {archive_date:%Y%m%d}：获取 {len(daily_ids)} 个。",
            file=sys.stderr,
        )
        for match_id in daily_ids:
            if match_id not in seen:
                seen.add(match_id)
                match_ids.append(match_id)
    return match_ids


def load_existing_match_ids(
    database_url: str,
    match_ids: Sequence[int],
) -> Set[int]:
    connection = psycopg2.connect(database_url)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(CREATE_MATCH_IDS_TABLE_SQL)
                if not match_ids:
                    return set()
                cursor.execute(
                    "SELECT match_id FROM match_ids WHERE match_id = ANY(%s)",
                    (list(match_ids),),
                )
                return {row[0] for row in cursor.fetchall()}
    finally:
        connection.close()


def insert_match_id_batch(
    database_url: str,
    match_ids: Sequence[int],
) -> int:
    connection = psycopg2.connect(database_url)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(CREATE_MATCH_IDS_TABLE_SQL)
                inserted_rows = execute_values(
                    cursor,
                    INSERT_MATCH_IDS_SQL,
                    [(match_id,) for match_id in match_ids],
                    fetch=True,
                )
                return len(inserted_rows)
    finally:
        connection.close()


def write_in_batches(
    database_url: str,
    match_ids: Sequence[int],
    batch_size: int,
    interval_seconds: float,
    writer: Callable[[str, Sequence[int]], int] = insert_match_id_batch,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    total_inserted = 0
    batches = [
        match_ids[index : index + batch_size]
        for index in range(0, len(match_ids), batch_size)
    ]
    for batch_number, batch in enumerate(batches, start=1):
        inserted = writer(database_url, batch)
        total_inserted += inserted
        print(
            f"{TASK_PREFIX} 第 {batch_number}/{len(batches)} 轮："
            f"提交 {len(batch)} 个，新增 {inserted} 个。",
            file=sys.stderr,
        )
        if batch_number < len(batches):
            sleeper(interval_seconds)
    return total_inserted


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv(ENV_FILE)
    args = parse_args(argv)
    database_url = os.environ.get(DATABASE_ENV_NAME, "").strip()
    if not database_url:
        print(f"{TASK_PREFIX} 缺少 {DATABASE_ENV_NAME}。", file=sys.stderr)
        return 2

    try:
        batch_size = env_number(
            BATCH_SIZE_ENV_NAME,
            DEFAULT_BATCH_SIZE,
            integer=True,
        )
        interval_seconds = env_number(
            INTERVAL_ENV_NAME,
            DEFAULT_INTERVAL_SECONDS,
        )
        match_ids = collect_match_ids(args.start_date, args.end_date)
        existing_ids = load_existing_match_ids(database_url, match_ids)
        new_ids = [match_id for match_id in match_ids if match_id not in existing_ids]
        print(
            f"{TASK_PREFIX} 汇总 {len(match_ids)} 个唯一 ID，"
            f"已存在 {len(existing_ids)} 个，待写入 {len(new_ids)} 个。",
            file=sys.stderr,
        )
        inserted = write_in_batches(
            database_url,
            new_ids,
            batch_size,
            interval_seconds,
        )
    except Exception as error:
        print(f"{TASK_PREFIX} 执行失败：{error}", file=sys.stderr)
        return 1

    print(f"{TASK_PREFIX} 完成，共新增 {inserted} 个比赛 ID。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
