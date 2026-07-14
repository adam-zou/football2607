"""``fetch-matches`` 命令行入口：抓一次比赛列表并输出 JSON。"""

import argparse
import asyncio
import json
import sys
from typing import List, Optional

from dotenv import load_dotenv

from .providers import Titan007Provider
from .proxy import ProxyManager


def build_parser() -> argparse.ArgumentParser:
    """声明命令行参数；这里只描述输入，不执行抓取。"""

    parser = argparse.ArgumentParser(description="Fetch football match lists")
    parser.add_argument(
        "--source",
        choices=["titan007"],
        default="titan007",
        help="data source (default: titan007)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (useful when diagnosing anti-bot checks)",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """组装依赖并执行一次异步抓取。"""

    proxy_manager = ProxyManager.from_env()
    provider = Titan007Provider(
        headless=not args.headed,
        proxy_manager=proxy_manager,
    )
    matches = await provider.fetch_matches()
    payload = [match.to_dict() for match in matches]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """同步入口：加载配置，并用 ``asyncio.run`` 启动异步世界。"""

    # 代理账号等敏感配置来自本地 .env，不硬编码进源码。
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        # 130 是命令行程序被 Ctrl+C 中断时的惯用退出码。
        return 130
    except Exception as error:
        print(f"fetch failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # 既支持安装后的 fetch-matches 命令，也支持 python -m fetch_data.cli。
    raise SystemExit(main())
