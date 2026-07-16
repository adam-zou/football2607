import argparse
import os
import unittest
from unittest.mock import patch

from crawl_status import env_active_crawl_statuses


class CrawlStatusConfigTests(unittest.TestCase):
    def test_defaults_to_only_unfinished(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            statuses = env_active_crawl_statuses(argparse.ArgumentParser())

        self.assertEqual(statuses, ["未完成"])

    def test_accepts_configured_supported_statuses_without_duplicates(self) -> None:
        with patch.dict(
            os.environ,
            {"SIMPLE_CRAWLER_ACTIVE_CRAWL_STATUSES": "未完成,异常,未完成"},
            clear=True,
        ):
            statuses = env_active_crawl_statuses(argparse.ArgumentParser())

        self.assertEqual(statuses, ["未完成", "异常"])


if __name__ == "__main__":
    unittest.main()
