"""``fetch-odds`` 命令行入口：抓取一场比赛的三类赔率变化。"""

import argparse
import asyncio
import json
import sys
from typing import List, Optional

from dotenv import load_dotenv

from .providers import Titan007OddsProvider
from .proxy import ProxyManager


def build_parser() -> argparse.ArgumentParser:
    """声明比赛 ID、机构、并发量等命令行参数。"""

    parser = argparse.ArgumentParser(
        description="Fetch Titan007 odds changes for one match"
    )
    parser.add_argument("match_id", type=int, help="Titan007 match ID")
    parser.add_argument(
        "--company-id",
        action="append",
        dest="company_ids",
        type=int,
        choices=list(Titan007OddsProvider.COMPANIES),
        help="fetch only this company; repeat for multiple companies",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (useful when diagnosing anti-bot checks)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="page timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=6,
        help="maximum pages fetched concurrently (default: 6)",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """创建代理和赔率 Provider，最终把快照输出为 JSON。"""

    proxy_manager = ProxyManager.from_env()
    provider = Titan007OddsProvider(
        headless=not args.headed,
        timeout_ms=int(args.timeout * 1000),
        max_concurrency=args.concurrency,
        proxy_manager=proxy_manager,
    )
    snapshot = await provider.fetch_match_odds(
        args.match_id,
        company_ids=args.company_ids,
    )
    print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
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
        print(f"odds fetch failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
