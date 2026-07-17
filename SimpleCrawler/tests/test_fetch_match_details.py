import argparse
import unittest
from contextlib import contextmanager
from unittest import mock

from fetch_match_details import (
    MatchDetail,
    block_unneeded_resources,
    fetch_detail_with_retries,
    parse_args,
    select_match_ids,
)


class FakeRequest:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class FakeRoute:
    def __init__(self, resource_type: str) -> None:
        self.request = FakeRequest(resource_type)
        self.action = None

    def abort(self) -> None:
        self.action = "abort"

    def continue_(self) -> None:
        self.action = "continue"


class ResourceBlockingTests(unittest.TestCase):
    def test_allows_scripts_that_populate_score_and_status(self) -> None:
        route = FakeRoute("script")

        block_unneeded_resources(route)

        self.assertEqual(route.action, "continue")

    def test_still_blocks_heavy_static_resources(self) -> None:
        for resource_type in ("stylesheet", "image", "media", "font"):
            with self.subTest(resource_type=resource_type):
                route = FakeRoute(resource_type)

                block_unneeded_resources(route)

                self.assertEqual(route.action, "abort")


class DetailConcurrencyArgumentTests(unittest.TestCase):
    def test_defaults_to_two_workers(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            args = parse_args([])

        self.assertEqual(args.concurrency, 2)

    def test_cli_overrides_environment(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"SIMPLE_CRAWLER_DETAIL_CONCURRENCY": "3"},
            clear=True,
        ):
            args = parse_args(["--concurrency", "5"])

        self.assertEqual(args.concurrency, 5)


class FakeCursor:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def execute(self, statement, parameters=None) -> None:
        self.executions.append((statement, parameters))

    def executemany(self, statement, parameters) -> None:
        self.executions.append((statement, parameters))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows) -> None:
        self.cursor_instance = FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1


class DetailMatchSelectionTests(unittest.TestCase):
    def test_normal_selection_applies_detail_refresh_window(self) -> None:
        connection = FakeConnection([(101,), (102,)])

        selected = select_match_ids(connection, [], None, ["未完成"])

        self.assertEqual(selected, [101, 102])
        statement, parameters = connection.cursor_instance.executions[-1]
        self.assertIn("LEFT JOIN match_details AS details", statement)
        self.assertIn("details.match_id IS NULL", statement)
        self.assertIn("details.status_text <> '完'", statement)
        self.assertIn("NOW() - INTERVAL '4 hours'", statement)
        self.assertIn("NOW() + INTERVAL '30 minutes'", statement)
        self.assertIn("NOW() - INTERVAL '1 minute'", statement)
        self.assertEqual(parameters, (["未完成"],))

    def test_normal_selection_prioritizes_missing_details(self) -> None:
        connection = FakeConnection([(101,)])

        select_match_ids(connection, [], 1, ["未完成"])

        statement, parameters = connection.cursor_instance.executions[-1]
        self.assertIn("ORDER BY (details.match_id IS NULL) DESC", statement)
        self.assertIn("LIMIT %s", statement)
        self.assertEqual(parameters, (["未完成"], 1))

    def test_explicit_ids_force_refresh_with_only_status_filter(self) -> None:
        connection = FakeConnection([(102,)])

        selected = select_match_ids(
            connection,
            [101, 102],
            None,
            ["未完成"],
        )

        self.assertEqual(selected, [102])
        statement, _ = connection.cursor_instance.executions[-1]
        self.assertNotIn("JOIN match_details", statement)
        self.assertNotIn("INTERVAL", statement)


class FakeProxy:
    def playwright_options(self):
        return {}


class FakeProxyClient:
    ttl_seconds = 30

    def __init__(self) -> None:
        self.leases = 0
        self.failed = 0
        self.succeeded = 0

    @contextmanager
    def lease(self, *, min_remaining_seconds):
        self.leases += 1
        try:
            yield FakeProxy()
        except BaseException:
            self.failed += 1
            raise
        else:
            self.succeeded += 1


class FakeAsyncPage:
    async def route(self, pattern, handler) -> None:
        return None


class FakeAsyncContext:
    def __init__(self) -> None:
        self.closed = False

    async def new_page(self):
        return FakeAsyncPage()

    async def close(self) -> None:
        self.closed = True


class FakeAsyncBrowser:
    def __init__(self) -> None:
        self.contexts = []

    async def new_context(self, **kwargs):
        context = FakeAsyncContext()
        self.contexts.append(context)
        return context


class DetailRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(
            timeout=10.0,
            url_template="https://example.test/{match_id}",
        )
        self.detail = MatchDetail(
            match_id=123,
            league="测试联赛",
            home_team="主队",
            away_team="客队",
            scheduled_time="2026-07-17 00:00",
            home_score=None,
            away_score=None,
            status_text="未开始",
        )

    @mock.patch(
        "fetch_match_details.fetch_detail_async",
        new_callable=mock.AsyncMock,
    )
    async def test_two_failures_switch_proxy_and_third_attempt_succeeds(
        self,
        fetch_detail,
    ) -> None:
        fetch_detail.side_effect = [
            RuntimeError("proxy one"),
            RuntimeError("proxy two"),
            self.detail,
        ]
        browser = FakeAsyncBrowser()
        proxy_client = FakeProxyClient()

        detail = await fetch_detail_with_retries(
            browser,
            proxy_client,
            self.args,
            123,
        )

        self.assertEqual(detail, self.detail)
        self.assertEqual(proxy_client.leases, 3)
        self.assertEqual(proxy_client.failed, 2)
        self.assertEqual(proxy_client.succeeded, 1)
        self.assertEqual(fetch_detail.await_count, 3)
        self.assertTrue(all(context.closed for context in browser.contexts))

    @mock.patch(
        "fetch_match_details.fetch_detail_async",
        new_callable=mock.AsyncMock,
    )
    async def test_three_failures_raise_the_last_error(
        self,
        fetch_detail,
    ) -> None:
        fetch_detail.side_effect = [
            RuntimeError("one"),
            RuntimeError("two"),
            RuntimeError("three"),
        ]
        browser = FakeAsyncBrowser()
        proxy_client = FakeProxyClient()

        with self.assertRaisesRegex(RuntimeError, "three"):
            await fetch_detail_with_retries(
                browser,
                proxy_client,
                self.args,
                123,
            )

        self.assertEqual(proxy_client.leases, 3)
        self.assertEqual(proxy_client.failed, 3)
        self.assertEqual(proxy_client.succeeded, 0)
        self.assertEqual(fetch_detail.await_count, 3)
        self.assertTrue(all(context.closed for context in browser.contexts))


if __name__ == "__main__":
    unittest.main()
