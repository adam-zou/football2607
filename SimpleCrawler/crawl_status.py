"""Shared crawl-status scope configuration for standalone crawler scripts."""

import argparse
import os
from typing import List


ACTIVE_STATUSES_ENV_NAME = "SIMPLE_CRAWLER_ACTIVE_CRAWL_STATUSES"
SUPPORTED_CRAWL_STATUSES = ("未完成", "已完成", "暂停爬取", "异常")
DEFAULT_ACTIVE_CRAWL_STATUSES = ["未完成"]


def env_active_crawl_statuses(parser: argparse.ArgumentParser) -> List[str]:
    raw = os.environ.get(ACTIVE_STATUSES_ENV_NAME)
    if raw is None or not raw.strip():
        return DEFAULT_ACTIVE_CRAWL_STATUSES.copy()
    statuses = list(
        dict.fromkeys(value.strip() for value in raw.split(",") if value.strip())
    )
    unsupported = [
        status for status in statuses if status not in SUPPORTED_CRAWL_STATUSES
    ]
    if not statuses or unsupported:
        parser.error(
            f"{ACTIVE_STATUSES_ENV_NAME} 只支持逗号分隔的"
            "未完成、已完成、暂停爬取、异常"
        )
    return statuses
