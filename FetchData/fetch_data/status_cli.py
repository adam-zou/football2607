"""``sync-match-status`` 命令行入口：持续同步比赛数据到 PostgreSQL。"""

import argparse
import asyncio
import logging
import os
import sys
from typing import List, Optional

from dotenv import load_dotenv

from .observability import RuntimeObservability
from .odds_postgres import PostgresOddsStore
from .postgres import PostgresMatchStore
from .providers import (
    Titan007MatchDetailProvider,
    Titan007OddsProvider,
    Titan007Provider,
)
from .proxy import ProxyManager
from .status_sync import MatchSynchronizer


def build_parser() -> argparse.ArgumentParser:
    """声明数据库连接、轮询间隔和详情批次大小。"""

    parser = argparse.ArgumentParser(
        description="持续将 Titan007 比赛和赔率同步到 PostgreSQL"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL 连接地址；默认读取 DATABASE_URL",
    )
    parser.add_argument(
        "--list-refresh-seconds",
        type=float,
        default=60.0,
        help="比赛列表刷新间隔（默认：60 秒）",
    )
    parser.add_argument(
        "--detail-refresh-seconds",
        type=float,
        default=60.0,
        help="比赛详情刷新间隔（默认：60 秒）",
    )
    parser.add_argument(
        "--detail-batch-size",
        type=int,
        default=10,
        help="每批保存的比赛详情数量（默认：10）",
    )
    parser.add_argument(
        "--dynamic-refresh-seconds",
        type=float,
        default=5.0,
        help="动态比赛队列空闲检查间隔（默认：5 秒）",
    )
    parser.add_argument(
        "--dynamic-batch-size",
        type=int,
        default=10,
        help="每次领取的动态比赛数量（默认：10）",
    )
    parser.add_argument(
        "--odds-refresh-seconds",
        type=float,
        default=5.0,
        help="赔率队列空闲检查间隔（默认：5 秒）",
    )
    parser.add_argument(
        "--odds-batch-size",
        type=int,
        default=6,
        help="每次补充到本地赔率队列的比赛数（默认：6）",
    )
    parser.add_argument(
        "--odds-match-concurrency",
        type=int,
        default=3,
        help="同时采集的比赛数（默认：3）",
    )
    parser.add_argument(
        "--odds-match-timeout-seconds",
        type=float,
        default=60.0,
        help="单场完整赔率采集总超时（默认：60 秒）",
    )
    parser.add_argument(
        "--odds-page-concurrency",
        type=int,
        default=12,
        help="全局同时采集的赔率页面数（默认：12）",
    )
    parser.add_argument(
        "--health-host",
        default="127.0.0.1",
        help="状态页和指标监听地址（默认：127.0.0.1）",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=8080,
        help="状态页和指标端口；0 表示关闭（默认：8080）",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="显示列表、详情和赔率采集使用的浏览器",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """在一个地方组装两个存储层、三个网页 Provider 和同步调度器。"""

    if not args.database_url:
        raise ValueError("必须提供 --database-url 或 DATABASE_URL")
    if not 0 <= args.health_port <= 65535:
        raise ValueError("--health-port 必须在 0 到 65535 之间")

    # 三个 Provider 共享同一个 ProxyManager，因此会复用代理及失败计数。
    observability = RuntimeObservability()
    proxy_manager = ProxyManager.from_env(observability=observability)
    synchronizer = MatchSynchronizer(
        store=PostgresMatchStore(args.database_url),
        match_list=Titan007Provider(
            headless=not args.headed,
            proxy_manager=proxy_manager,
            observability=observability,
        ),
        match_details=Titan007MatchDetailProvider(
            headless=not args.headed,
            proxy_manager=proxy_manager,
            observability=observability,
        ),
        odds_store=PostgresOddsStore(args.database_url),
        match_odds=Titan007OddsProvider(
            headless=not args.headed,
            max_concurrency=args.odds_page_concurrency,
            proxy_manager=proxy_manager,
            observability=observability,
        ),
        list_refresh_seconds=args.list_refresh_seconds,
        detail_refresh_seconds=args.detail_refresh_seconds,
        detail_batch_size=args.detail_batch_size,
        dynamic_refresh_seconds=args.dynamic_refresh_seconds,
        dynamic_batch_size=args.dynamic_batch_size,
        odds_refresh_seconds=args.odds_refresh_seconds,
        odds_batch_size=args.odds_batch_size,
        odds_match_concurrency=args.odds_match_concurrency,
        odds_match_timeout_seconds=args.odds_match_timeout_seconds,
        observability=observability,
    )
    server = None
    if args.health_port:
        server = await observability.start_server(args.health_host, args.health_port)
        logging.getLogger(__name__).info(
            "状态页和监控端点已启动：http://%s:%d",
            args.health_host,
            args.health_port,
        )
    try:
        # run() 是常驻循环，正常情况下会一直运行到用户按 Ctrl+C。
        await synchronizer.run()
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
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
        print(f"比赛数据同步失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
