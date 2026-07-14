import json
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fetch_data.models import HandicapChange, Movement, OneXTwoChange, OverUnderChange
from fetch_data.providers.titan007_odds import Titan007OddsProvider


def cell(text: str, color: str = "", col_span: int = 1):
    return {"text": text, "color": color, "colSpan": col_span}


class FakeProxy:
    def playwright_options(self):
        return {}


class FakeProxyManager:
    async def get_proxy(self):
        return FakeProxy()

    async def report_success(self):
        return None

    async def report_error(self):
        return None


class FakeBrowser:
    async def close(self):
        return None


class FakeChromium:
    async def launch(self, **kwargs):
        return FakeBrowser()


class FakePlaywrightContext:
    async def __aenter__(self):
        return type("Playwright", (), {"chromium": FakeChromium()})()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class Titan007OddsProviderTests(unittest.TestCase):
    def test_page_validation_rejects_error_and_unrecognized_pages(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "blocked or error page"):
            Titan007OddsProvider._validate_page_state(
                {
                    "title": "Access Denied",
                    "bodyText": "Request blocked by WAF",
                    "hasExpectedTable": False,
                    "hasMarketShell": False,
                    "hasMarketNavigation": False,
                }
            )

        with self.assertRaisesRegex(RuntimeError, "missing expected market structure"):
            Titan007OddsProvider._validate_page_state(
                {
                    "title": "Unexpected page",
                    "bodyText": "hello",
                    "hasExpectedTable": False,
                    "hasMarketShell": False,
                    "hasMarketNavigation": False,
                }
            )

    def test_page_validation_accepts_explicit_empty_market_shell(self) -> None:
        has_table = Titan007OddsProvider._validate_page_state(
            {
                "title": "赔率变化",
                "bodyText": "亚让 胜平负 进球数",
                "hasExpectedTable": False,
                "hasMarketShell": True,
                "hasMarketNavigation": True,
            }
        )

        self.assertFalse(has_table)

    def test_failed_page_discards_only_its_company(self) -> None:
        provider = Titan007OddsProvider(proxy_manager=FakeProxyManager())

        async def fetch_rows(browser, match_id, company_id, market):
            if company_id == 4 and market == "one_x_two":
                raise RuntimeError("temporary page failure")
            return []

        provider._fetch_page_rows = AsyncMock(side_effect=fetch_rows)

        with patch(
            "fetch_data.providers.titan007_odds.async_playwright",
            return_value=FakePlaywrightContext(),
        ):
            snapshot = asyncio.run(
                provider.fetch_match_odds(3020831, company_ids=[3, 4])
            )

        self.assertEqual(snapshot.companies, {3: "Crow*"})
        self.assertIn(4, snapshot.failed_companies)
        self.assertIn("one_x_two", snapshot.failed_companies[4])
        self.assertEqual(provider._fetch_page_rows.await_count, 6)

    def test_all_companies_failing_raises_collection_error(self) -> None:
        provider = Titan007OddsProvider(proxy_manager=FakeProxyManager())
        provider._fetch_page_rows = AsyncMock(side_effect=RuntimeError("failed"))

        with patch(
            "fetch_data.providers.titan007_odds.async_playwright",
            return_value=FakePlaywrightContext(),
        ):
            with self.assertRaisesRegex(RuntimeError, "all selected companies failed"):
                asyncio.run(provider.fetch_match_odds(3020831, company_ids=[3]))

    def test_missing_market_table_is_represented_by_no_rows(self) -> None:
        self.assertEqual(
            Titan007OddsProvider.parse_rows(
                "handicap", [], match_id=3020831, company_id=4
            ),
            [],
        )

    def test_build_url_changes_match_company_and_market(self) -> None:
        self.assertEqual(
            Titan007OddsProvider.build_url(3020831, 3, "over_under"),
            "https://vip.titan007.com/changeDetail/overunder.aspx"
            "?id=3020831&companyid=3&l=0",
        )

    def test_parse_over_under_preserves_dom_order_and_reverses_seq(self) -> None:
        rows = [
            {
                "cells": [
                    cell("77"),
                    cell("1-1"),
                    cell("1.08", "red"),
                    cell("2.5"),
                    cell("0.73", "green"),
                    cell("7-13 22:20"),
                    cell("滚"),
                ]
            },
            {
                "cells": [
                    cell("77"),
                    cell("1-1"),
                    cell("1.05"),
                    cell("2/2.5", "red"),
                    cell("0.75"),
                    cell("7-13 22:20"),
                    cell("滚"),
                ]
            },
        ]

        changes = Titan007OddsProvider.parse_rows(
            "over_under", rows, match_id=3020831, company_id=3
        )

        self.assertEqual([change.seq for change in changes], [2, 1])
        self.assertEqual([change.change_time for change in changes], [
            "7-13 22:20",
            "7-13 22:20",
        ])
        latest = changes[0]
        self.assertIsInstance(latest, OverUnderChange)
        assert isinstance(latest, OverUnderChange)
        self.assertEqual(latest.home_score, 1)
        self.assertEqual(latest.away_score, 1)
        self.assertEqual(latest.over_odds_movement, Movement.UP)
        self.assertEqual(latest.total_line_movement, Movement.UNCHANGED)
        self.assertEqual(latest.under_odds_movement, Movement.DOWN)
        self.assertEqual(changes[1].total_line_value, 2.25)

    def test_parse_suspended_handicap_sets_market_values_to_null(self) -> None:
        changes = Titan007OddsProvider.parse_rows(
            "handicap",
            [
                {
                    "cells": [
                        cell("14"),
                        cell("0-0"),
                        cell("封", "green", 3),
                        cell("7-13 20:59"),
                        cell("滚"),
                    ]
                }
            ],
            match_id=3020831,
            company_id=8,
        )

        change = changes[0]
        self.assertIsInstance(change, HandicapChange)
        assert isinstance(change, HandicapChange)
        self.assertTrue(change.is_suspended)
        self.assertIsNone(change.home_odds)
        self.assertIsNone(change.home_odds_movement)
        self.assertIsNone(change.handicap_raw)
        self.assertIsNone(change.handicap_value)
        self.assertIsNone(change.handicap_movement)
        self.assertIsNone(change.away_odds)
        self.assertIsNone(change.away_odds_movement)

    def test_parse_prematch_one_x_two_uses_null_time_and_scores(self) -> None:
        changes = Titan007OddsProvider.parse_rows(
            "one_x_two",
            [
                {
                    "cells": [
                        cell(""),
                        cell(""),
                        cell("2.10"),
                        cell("2.80"),
                        cell("3.50"),
                        cell("07-12 06:54"),
                        cell("(初盘)"),
                    ]
                }
            ],
            match_id=3020831,
            company_id=8,
        )

        change = changes[0]
        self.assertIsInstance(change, OneXTwoChange)
        assert isinstance(change, OneXTwoChange)
        self.assertIsNone(change.match_minute)
        self.assertIsNone(change.home_score)
        self.assertIsNone(change.away_score)
        self.assertEqual(change.change_time, "07-12 06:54")
        self.assertEqual(change.source_status, "(初盘)")

    def test_convert_handicap_and_total_lines(self) -> None:
        self.assertEqual(Titan007OddsProvider.parse_handicap_value("平手"), 0)
        self.assertEqual(Titan007OddsProvider.parse_handicap_value("半球/一球"), 0.75)
        self.assertEqual(Titan007OddsProvider.parse_handicap_value("受平手/半球"), -0.25)
        self.assertIsNone(Titan007OddsProvider.parse_handicap_value("未知盘口"))
        self.assertEqual(Titan007OddsProvider.parse_total_line_value("1/1.5"), 1.25)
        self.assertIsNone(Titan007OddsProvider.parse_total_line_value("未知盘口"))

    def test_model_output_is_json_serializable_with_chinese_movements(self) -> None:
        changes = Titan007OddsProvider.parse_rows(
            "over_under",
            [
                {
                    "cells": [
                        cell(""), cell(""), cell("0.90", "red"),
                        cell("2"), cell("0.90", "green"),
                        cell("7-12 21:43"), cell("即"),
                    ]
                }
            ],
            match_id=3020831,
            company_id=3,
        )

        encoded = json.dumps(changes[0].to_dict(), ensure_ascii=False)
        self.assertIn('"over_odds_movement": "上升"', encoded)
        self.assertIn('"under_odds_movement": "下降"', encoded)


if __name__ == "__main__":
    unittest.main()
