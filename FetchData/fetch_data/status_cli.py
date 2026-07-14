"""``sync-match-status`` 命令行入口：持续同步比赛数据到 PostgreSQL。"""

import argparse
import asyncio
import logging
import os
import sys
from typing import List, Optional

from dotenv import load_dotenv

from .postgres import PostgresMatchStore
from .providers import Titan007MatchDetailProvider, Titan007Provider
from .proxy import ProxyManager
from .status_sync import MatchSynchronizer


def build_parser() -> argparse.ArgumentParser:
    """声明数据库连接、轮询间隔和详情批次大小。"""

    parser = argparse.ArgumentParser(
        description="Continuously synchronize titan007 matches to PostgreSQL"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN; defaults to DATABASE_URL",
    )
    parser.add_argument(
        "--list-refresh-seconds",
        type=float,
        default=60.0,
        help="match-list refresh interval (default: 60 seconds)",
    )
    parser.add_argument(
        "--detail-refresh-seconds",
        type=float,
        default=60.0,
        help="pending-detail refresh interval (default: 60 seconds)",
    )
    parser.add_argument(
        "--detail-batch-size",
        type=int,
        default=10,
        help="detail rows persisted per batch (default: 10)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browsers used for list and detail refreshes",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """在一个地方组装存储层、两个网页 Provider 和同步调度器。"""

    if not args.database_url:
        raise ValueError("--database-url or DATABASE_URL is required")

    # 两个 Provider 共享同一个 ProxyManager，因此会复用代理及失败计数。
    proxy_manager = ProxyManager.from_env()
    synchronizer = MatchSynchronizer(
        store=PostgresMatchStore(args.database_url),
        match_list=Titan007Provider(
            headless=not args.headed,
            proxy_manager=proxy_manager,
        ),
        match_details=Titan007MatchDetailProvider(
            headless=not args.headed,
            proxy_manager=proxy_manager,
        ),
        list_refresh_seconds=args.list_refresh_seconds,
        detail_refresh_seconds=args.detail_refresh_seconds,
        detail_batch_size=args.detail_batch_size,
    )
    # run() 是常驻循环，正常情况下会一直运行到用户按 Ctrl+C。
    await synchronizer.run()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """加载环境变量和日志配置，再进入异步同步流程。"""

    # 系统中已有的环境变量优先级高于 .env 中的值。
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"status synchronization failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
