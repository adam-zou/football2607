import unittest
from contextlib import contextmanager
from unittest import mock

from odds_collection import (
    BLOCKED_RESOURCE_TYPES,
    MARKETS,
    OddsCompanyJob,
    OddsCollectionConfig,
    OddsPageJob,
    collect_company_markets_async,
    collect_market_page,
    collect_market_page_async,
)


class FakeProxy:
    def playwright_options(self):
        return {"server": "http://proxy.test:8080"}


class FakeProxyClient:
    ttl_seconds = 30

    def __init__(self) -> None:
        self.leases = 0
        self.page_assignments = []
        self.minimum_remaining_seconds = []
        self.failed_leases = 0

    @contextmanager
    def lease(self, *, min_remaining_seconds, page_assignments=1):
        self.leases += 1
        self.page_assignments.append(page_assignments)
        self.minimum_remaining_seconds.append(min_remaining_seconds)
        try:
            yield FakeProxy()
        except BaseException:
            self.failed_leases += 1
            raise


class FakePage:
    def route(self, pattern, handler) -> None:
        self.route_args = (pattern, handler)


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()
        self.closed = False

    def new_page(self):
        return self.page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.options = None

    def new_context(self, **options):
        self.options = options
        return self.context


class FakeAsyncPage:
    async def route(self, pattern, handler) -> None:
        self.route_args = (pattern, handler)


class FakeAsyncContext:
    def __init__(self) -> None:
        self.page = FakeAsyncPage()
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakeAsyncBrowser:
    def __init__(self) -> None:
        self.context = FakeAsyncContext()
        self.options = None

    async def new_context(self, **options):
        self.options = options
        return self.context


class OddsCollectionTests(unittest.TestCase):
    def test_scripts_and_static_assets_are_blocked(self) -> None:
        self.assertEqual(
            BLOCKED_RESOURCE_TYPES,
            {"script", "stylesheet", "image", "media", "font"},
        )

    @mock.patch("odds_collection.Titan007OddsParser.parse_rows")
    @mock.patch("odds_collection.fetch_page_rows", return_value=[])
    def test_sync_collection_owns_one_proxy_context_and_parsing(
        self,
        fetch_page_rows,
        parse_rows,
    ) -> None:
        parse_rows.return_value = [mock.sentinel.change]
        browser = FakeBrowser()
        proxy_client = FakeProxyClient()
        config = OddsCollectionConfig(
            base_url="https://example.test/{endpoint}",
            timeout_seconds=12.0,
        )
        job = OddsPageJob(123, 3, "handicap")

        changes = collect_market_page(browser, proxy_client, config, job)

        self.assertEqual(changes, [mock.sentinel.change])
        self.assertEqual(proxy_client.leases, 1)
        self.assertTrue(browser.context.closed)
        fetch_page_rows.assert_called_once()
        parse_rows.assert_called_once_with(
            "handicap",
            [],
            match_id=123,
            company_id=3,
        )


class AsyncOddsCollectionTests(unittest.IsolatedAsyncioTestCase):
    @mock.patch("odds_collection.Titan007OddsParser.parse_rows")
    @mock.patch(
        "odds_collection.fetch_page_rows_async",
        new_callable=mock.AsyncMock,
        return_value=[],
    )
    async def test_async_collection_uses_the_same_page_semantics(
        self,
        fetch_page_rows,
        parse_rows,
    ) -> None:
        parse_rows.return_value = [mock.sentinel.change]
        browser = FakeAsyncBrowser()
        proxy_client = FakeProxyClient()
        config = OddsCollectionConfig(
            base_url="https://example.test/{endpoint}",
            timeout_seconds=12.0,
        )
        job = OddsPageJob(123, 3, "over_under")

        changes = await collect_market_page_async(
            browser,
            proxy_client,
            config,
            job,
        )

        self.assertEqual(changes, [mock.sentinel.change])
        self.assertEqual(proxy_client.leases, 1)
        self.assertTrue(browser.context.closed)
        fetch_page_rows.assert_awaited_once()
        parse_rows.assert_called_once_with(
            "over_under",
            [],
            match_id=123,
            company_id=3,
        )

    @mock.patch("odds_collection.Titan007OddsParser.parse_rows")
    @mock.patch(
        "odds_collection.fetch_page_rows_async",
        new_callable=mock.AsyncMock,
    )
    async def test_company_collection_reuses_one_context_for_three_markets(
        self,
        fetch_page_rows,
        parse_rows,
    ) -> None:
        fetch_page_rows.side_effect = [[{"cells": []}]] * 3
        parse_rows.side_effect = lambda market, *_args, **_kwargs: [market]
        browser = FakeAsyncBrowser()
        proxy_client = FakeProxyClient()
        config = OddsCollectionConfig(
            base_url="https://example.test/{endpoint}",
            timeout_seconds=5.0,
        )

        outcomes = await collect_company_markets_async(
            browser,
            proxy_client,
            config,
            OddsCompanyJob(123, 3, tuple(MARKETS)),
        )

        self.assertEqual([item.job.market for item in outcomes], list(MARKETS))
        self.assertTrue(all(item.error is None for item in outcomes))
        self.assertEqual(proxy_client.leases, 1)
        self.assertEqual(proxy_client.page_assignments, [3])
        self.assertTrue(browser.context.closed)
        self.assertEqual(fetch_page_rows.await_count, 3)

    @mock.patch("odds_collection.Titan007OddsParser.parse_rows")
    @mock.patch(
        "odds_collection.fetch_page_rows_async",
        new_callable=mock.AsyncMock,
        return_value=[],
    )
    async def test_company_lease_uses_current_proxy_without_duration_estimate(
        self,
        _fetch_page_rows,
        parse_rows,
    ) -> None:
        parse_rows.return_value = []
        proxy_client = FakeProxyClient()

        await collect_company_markets_async(
            FakeAsyncBrowser(),
            proxy_client,
            OddsCollectionConfig(
                base_url="https://example.test/{endpoint}",
                timeout_seconds=12.0,
            ),
            OddsCompanyJob(123, 3, tuple(MARKETS)),
        )

        self.assertEqual(proxy_client.minimum_remaining_seconds, [1.0])
        self.assertEqual(proxy_client.page_assignments, [3])

    @mock.patch("odds_collection.Titan007OddsParser.parse_rows")
    @mock.patch(
        "odds_collection.fetch_page_rows_async",
        new_callable=mock.AsyncMock,
    )
    async def test_company_collection_preserves_successes_after_one_failure(
        self,
        fetch_page_rows,
        parse_rows,
    ) -> None:
        fetch_page_rows.side_effect = [
            [{"cells": []}],
            RuntimeError("one market failed"),
            [{"cells": []}],
        ]
        parse_rows.return_value = [mock.sentinel.change]
        browser = FakeAsyncBrowser()
        proxy_client = FakeProxyClient()

        outcomes = await collect_company_markets_async(
            browser,
            proxy_client,
            OddsCollectionConfig(
                base_url="https://example.test/{endpoint}",
                timeout_seconds=5.0,
            ),
            OddsCompanyJob(123, 3, tuple(MARKETS)),
        )

        self.assertIsNone(outcomes[0].error)
        self.assertRegex(str(outcomes[1].error), "one market failed")
        self.assertIsNone(outcomes[2].error)
        self.assertEqual(proxy_client.failed_leases, 1)


if __name__ == "__main__":
    unittest.main()
