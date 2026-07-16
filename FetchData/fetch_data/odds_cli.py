"""``fetch-odds`` 命令行入口：抓取一场比赛的三类赔率变化。"""

import argparse
import asyncio
import os
import sys
from typing import List, Optional

from dotenv import load_dotenv

from .odds_postgres import PostgresOddsStore
from .providers import Titan007OddsProvider
from .proxy import ProxyManager


def build_parser() -> argparse.ArgumentParser:
    """声明比赛 ID、机构、并发量等命令行参数。"""

    parser = argparse.ArgumentParser(
        description="抓取一场 Titan007 比赛的赔率变化并写入 PostgreSQL"
    )
    parser.add_argument("match_id", type=int, help="Titan007 比赛 ID")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL 连接地址；默认读取 DATABASE_URL",
    )
    parser.add_argument(
        "--company-id",
        action="append",
        dest="company_ids",
        type=int,
        choices=list(Titan007OddsProvider.COMPANIES),
        help="只抓取指定公司；可重复传入多个公司",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="显示浏览器窗口，用于排查反爬拦截",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="单个页面超时秒数（默认：10）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=6,
        help="最大页面并发数（默认：6）",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """抓取赔率快照，并在一个数据库事务中写入三张表。"""

    if not args.database_url:
        raise ValueError("必须提供 --database-url 或 DATABASE_URL")

    proxy_manager = ProxyManager.from_env()
    provider = Titan007OddsProvider(
        headless=not args.headed,
        timeout_ms=int(args.timeout * 1000),
        max_concurrency=args.concurrency,
        proxy_manager=proxy_manager,
    )
    store = PostgresOddsStore(args.database_url)
    await store.initialize()
    try:
        snapshot = await provider.fetch_match_odds(
            args.match_id,
            company_ids=args.company_ids,
        )
        await store.upsert_snapshot(snapshot)
    finally:
        await store.close()

    print(
        "赔率变化已保存："
        f"比赛ID={snapshot.match_id} "
        f"成功页面={len(snapshot.successful_markets)} "
        f"失败页面="
        f"{','.join(f'{item.company_id}/{item.market}' for item in snapshot.failed_markets) or '-'} "
        f"亚让={len(snapshot.handicap_changes)} "
        f"胜平负={len(snapshot.one_x_two_changes)} "
        f"进球数={len(snapshot.over_under_changes)}"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """命令行的同步外壳，负责配置、异常和进程退出码。"""

    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        # 用户主动按 Ctrl+C 不打印错误堆栈，直接返回标准中断码。
        return 130
    except Exception as error:
        print(f"赔率采集失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
