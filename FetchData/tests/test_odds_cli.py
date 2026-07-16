import argparse
import asyncio
import unittest
from unittest.mock import patch

from fetch_data.models import OddsSnapshot
from fetch_data.odds_cli import build_parser, run


class OddsCliTests(unittest.TestCase):
    def test_match_id_is_required_and_all_companies_are_default(self) -> None:
        args = build_parser().parse_args(["3020831"])

        self.assertEqual(args.match_id, 3020831)
        self.assertIsNone(args.company_ids)
        self.assertEqual(args.timeout, 10.0)
        self.assertEqual(args.concurrency, 6)

    def test_company_filter_can_be_repeated(self) -> None:
        args = build_parser().parse_args(
            ["3020831", "--company-id", "3", "--company-id", "47"]
        )

        self.assertEqual(args.company_ids, [3, 47])

    def test_database_url_can_be_supplied(self) -> None:
        args = build_parser().parse_args(
            ["3020831", "--database-url", "postgresql://example/football"]
        )

        self.assertEqual(args.database_url, "postgresql://example/football")

    def test_run_persists_snapshot_and_closes_store(self) -> None:
        snapshot = OddsSnapshot(
            match_id=3020831,
            companies={3: "Crow*"},
            handicap_changes=[],
            one_x_two_changes=[],
            over_under_changes=[],
        )

        class FakeProvider:
            async def fetch_match_odds(self, match_id, company_ids=None):
                self.request = (match_id, company_ids)
                return snapshot

        class FakeStore:
            def __init__(self) -> None:
                self.initialized = False
                self.closed = False
                self.snapshot = None

            async def initialize(self) -> None:
                self.initialized = True

            async def upsert_snapshot(self, value) -> None:
                self.snapshot = value

            async def close(self) -> None:
                self.closed = True

        provider = FakeProvider()
        store = FakeStore()
        args = argparse.Namespace(
            match_id=3020831,
            database_url="postgresql://example/football",
            company_ids=[3],
            headed=False,
            timeout=30,
            concurrency=6,
        )

        with patch("fetch_data.odds_cli.ProxyManager.from_env", return_value=object()):
            with patch(
                "fetch_data.odds_cli.Titan007OddsProvider",
                return_value=provider,
            ):
                with patch(
                    "fetch_data.odds_cli.PostgresOddsStore",
                    return_value=store,
                ):
                    with patch("builtins.print") as output:
                        result = asyncio.run(run(args))

        self.assertEqual(result, 0)
        self.assertTrue(store.initialized)
        self.assertIs(store.snapshot, snapshot)
        self.assertTrue(store.closed)
        self.assertEqual(provider.request, (3020831, [3]))
        self.assertIn("赔率变化已保存", output.call_args.args[0])
        self.assertIn("成功页面=3", output.call_args.args[0])
        self.assertIn("失败页面=-", output.call_args.args[0])

    def test_run_requires_database_url(self) -> None:
        args = argparse.Namespace(database_url=None)

        with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            asyncio.run(run(args))


if __name__ == "__main__":
    unittest.main()
