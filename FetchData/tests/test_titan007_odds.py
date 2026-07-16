import json
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fetch_data.models import (
    HandicapChange,
    Movement,
    OddsMarketRequest,
    OneXTwoChange,
    OverUnderChange,
)
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

    async def force_refresh(self):
        return FakeProxy()


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
    def test_forced_proxy_refresh_is_delegated_to_proxy_manager(self) -> None:
        proxy_manager = FakeProxyManager()
        proxy_manager.force_refresh = AsyncMock(return_value=FakeProxy())
        provider = Titan007OddsProvider(proxy_manager=proxy_manager)

        asyncio.run(provider.refresh_proxy())

        proxy_manager.force_refresh.assert_awaited_once()

    def test_page_validation_rejects_error_and_unrecognized_pages(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "拦截页或错误页"):
            Titan007OddsProvider._validate_page_state(
                {
                    "title": "Access Denied",
                    "bodyText": "Request blocked by WAF",
                    "hasExpectedTable": False,
                    "hasMarketShell": False,
                    "hasMarketNavigation": False,
                }
            )

        with self.assertRaisesRegex(RuntimeError, "缺少预期的市场结构"):
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

    def test_page_wait_passes_selector_as_keyword_argument(self) -> None:
        class FakeResponse:
            status = 200

        class KeywordOnlyPage:
            def __init__(self) -> None:
                self.wait_argument = None

            async def goto(self, url, *, wait_until, timeout):
                return FakeResponse()

            async def wait_for_function(
                self,
                expression,
                *,
                arg=None,
                polling=None,
                timeout=None,
            ):
                self.wait_argument = arg

            async def evaluate(self, expression, arg):
                return {
                    "title": "赔率变化",
                    "bodyText": "亚让 胜平负 进球数",
                    "hasExpectedTable": False,
                    "hasMarketShell": True,
                    "hasMarketNavigation": True,
                }

            async def close(self):
                return None

        class BrowserWithPage:
            def __init__(self, page) -> None:
                self.page = page

            async def new_page(self, **kwargs):
                return self.page

        provider = Titan007OddsProvider(proxy_manager=FakeProxyManager())
        page = KeywordOnlyPage()

        rows = asyncio.run(
            provider._fetch_page_rows(
                BrowserWithPage(page),
                3020831,
                3,
                "handicap",
            )
        )

        self.assertEqual(rows, [])
        self.assertEqual(page.wait_argument, "#odds2 table")

    def test_failed_page_preserves_other_markets_from_same_company(self) -> None:
        proxy_manager = FakeProxyManager()
        proxy_manager.report_success = AsyncMock()
        proxy_manager.report_error = AsyncMock()
        provider = Titan007OddsProvider(proxy_manager=proxy_manager)

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

        self.assertEqual(snapshot.companies, {3: "Crow*", 4: "立*"})
        self.assertEqual(len(snapshot.successful_markets), 5)
        self.assertEqual(
            set(snapshot.failed_markets),
            {OddsMarketRequest(4, "one_x_two")},
        )
        self.assertEqual(provider._fetch_page_rows.await_count, 6)
        proxy_manager.report_success.assert_awaited_once()
        proxy_manager.report_error.assert_not_awaited()

    def test_all_pages_failing_returns_individual_failure_results(self) -> None:
        proxy_manager = FakeProxyManager()
        proxy_manager.report_success = AsyncMock()
        proxy_manager.report_error = AsyncMock()
        provider = Titan007OddsProvider(proxy_manager=proxy_manager)
        provider._fetch_page_rows = AsyncMock(side_effect=RuntimeError("failed"))

        with patch(
            "fetch_data.providers.titan007_odds.async_playwright",
            return_value=FakePlaywrightContext(),
        ):
            snapshot = asyncio.run(
                provider.fetch_match_odds(3020831, company_ids=[3])
            )

        self.assertEqual(snapshot.companies, {})
        self.assertEqual(len(snapshot.successful_markets), 0)
        self.assertEqual(len(snapshot.failed_markets), 3)
        proxy_manager.report_error.assert_awaited_once()
        proxy_manager.report_success.assert_not_awaited()

    def test_explicit_market_requests_fetch_only_failed_page(self) -> None:
        provider = Titan007OddsProvider(proxy_manager=FakeProxyManager())
        provider._fetch_page_rows = AsyncMock(return_value=[])
        request = OddsMarketRequest(4, "over_under")

        with patch(
            "fetch_data.providers.titan007_odds.async_playwright",
            return_value=FakePlaywrightContext(),
        ):
            snapshot = asyncio.run(
                provider.fetch_match_odds(
                    3020831,
                    market_requests=[request],
                )
            )

        provider._fetch_page_rows.assert_awaited_once_with(
            unittest.mock.ANY,
            3020831,
            4,
            "over_under",
        )
        self.assertEqual(snapshot.successful_markets, (request,))

    def test_page_concurrency_is_global_across_matches(self) -> None:
        provider = Titan007OddsProvider(
            proxy_manager=FakeProxyManager(),
            max_concurrency=2,
        )
        active = 0
        maximum_active = 0
        original_sleep = asyncio.sleep

        async def fetch_rows(browser, match_id, company_id, market):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await original_sleep(0)
            active -= 1
            return []

        provider._fetch_page_rows = AsyncMock(side_effect=fetch_rows)

        async def fetch_two_matches():
            return await asyncio.gather(
                provider.fetch_match_odds(3020831, company_ids=[3]),
                provider.fetch_match_odds(3020832, company_ids=[3]),
            )

        with patch(
            "fetch_data.providers.titan007_odds.async_playwright",
            return_value=FakePlaywrightContext(),
        ):
            asyncio.run(fetch_two_matches())

        self.assertEqual(maximum_active, 2)
        self.assertEqual(provider._fetch_page_rows.await_count, 6)

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
