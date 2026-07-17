import argparse
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fetch_match_ids import (
    extract_match_ids,
    extract_match_ids_from_bfdata,
    fetch_match_ids_with_retries,
    route_list_resource,
)


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


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def new_page(self):
        return object()

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts = []

    def new_context(self, **kwargs):
        context = FakeContext()
        self.contexts.append(context)
        return context


class FakeResponse:
    def __init__(self, url, text, status=200) -> None:
        self.url = url
        self._text = text
        self.status = status

    def text(self):
        return self._text


class FakeResponseInfo:
    def __init__(self, response) -> None:
        self.value = response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeListPage:
    def __init__(self, data_response) -> None:
        self.data_response = data_response
        self.route_args = None
        self.response_predicate = None
        self.goto_args = None

    def route(self, pattern, handler) -> None:
        self.route_args = (pattern, handler)

    def expect_response(self, predicate, timeout):
        self.response_predicate = predicate
        self.response_timeout = timeout
        return FakeResponseInfo(self.data_response)

    def goto(self, url, wait_until, timeout):
        self.goto_args = (url, wait_until, timeout)
        return FakeResponse(url, "<html></html>")


class FakeRoute:
    def __init__(self, resource_type) -> None:
        self.request = argparse.Namespace(resource_type=resource_type)
        self.action = None

    def abort(self) -> None:
        self.action = "abort"

    def continue_(self) -> None:
        self.action = "continue"


class MatchDataResponseTests(unittest.TestCase):
    def test_blocks_static_resources_but_keeps_data_dependencies(self) -> None:
        for resource_type in ("image", "stylesheet", "media", "font"):
            with self.subTest(resource_type=resource_type):
                route = FakeRoute(resource_type)
                route_list_resource(route)
                self.assertEqual(route.action, "abort")

        for resource_type in ("document", "script", "xhr", "websocket"):
            with self.subTest(resource_type=resource_type):
                route = FakeRoute(resource_type)
                route_list_resource(route)
                self.assertEqual(route.action, "continue")

    def test_extracts_ids_and_validates_declared_match_count(self) -> None:
        source = """
            var A=Array(3); var matchcount=2;
            A[1]="3000002^league^home^away".split('^');
            A[2]="3000001^league^home^away".split('^');
        """

        self.assertEqual(
            extract_match_ids_from_bfdata(source),
            [3000001, 3000002],
        )

    def test_rejects_partial_or_changed_data_format(self) -> None:
        source = """
            var A=Array(3); var matchcount=2;
            A[1]="3000001^league".split('^');
        """

        with self.assertRaisesRegex(RuntimeError, "数量不一致"):
            extract_match_ids_from_bfdata(source)

    def test_reads_bfdata_response_without_querying_dom(self) -> None:
        source = """
            var A=Array(2); var matchcount=1;
            A[1]="3000001^league".split('^');
        """
        response = FakeResponse(
            "https://livestatic.titan007.com/vbsxml/bfdata_ut.js?r=1",
            source,
        )
        page = FakeListPage(response)

        match_ids = extract_match_ids(
            page,
            "https://live.titan007.com/oldIndexall.aspx",
            15000,
            1000,
        )

        self.assertEqual(match_ids, [3000001])
        self.assertEqual(page.route_args[0], "**/*")
        self.assertTrue(page.response_predicate(response))
        self.assertEqual(page.response_timeout, 15000)
        self.assertEqual(
            page.goto_args,
            (
                "https://live.titan007.com/oldIndexall.aspx",
                "commit",
                15000,
            ),
        )


class MatchIdRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(
            timeout=10.0,
            settle=1.0,
            url="https://example.test/list",
        )

    @patch(
        "fetch_match_ids.extract_match_ids",
        side_effect=[RuntimeError("proxy one"), RuntimeError("proxy two"), [1, 2]],
    )
    def test_two_failures_switch_proxy_and_third_attempt_succeeds(
        self,
        extract,
    ) -> None:
        browser = FakeBrowser()
        proxy_client = FakeProxyClient()

        match_ids = fetch_match_ids_with_retries(
            browser,
            proxy_client,
            self.args,
        )

        self.assertEqual(match_ids, [1, 2])
        self.assertEqual(proxy_client.leases, 3)
        self.assertEqual(proxy_client.failed, 2)
        self.assertEqual(proxy_client.succeeded, 1)
        self.assertEqual(extract.call_count, 3)
        self.assertTrue(all(context.closed for context in browser.contexts))

    @patch(
        "fetch_match_ids.extract_match_ids",
        side_effect=[RuntimeError("one"), RuntimeError("two"), RuntimeError("three")],
    )
    def test_three_failures_raise_the_last_error(self, extract) -> None:
        browser = FakeBrowser()
        proxy_client = FakeProxyClient()

        with self.assertRaisesRegex(RuntimeError, "three"):
            fetch_match_ids_with_retries(browser, proxy_client, self.args)

        self.assertEqual(proxy_client.leases, 3)
        self.assertEqual(proxy_client.failed, 3)
        self.assertEqual(proxy_client.succeeded, 0)
        self.assertEqual(extract.call_count, 3)
        self.assertTrue(all(context.closed for context in browser.contexts))


if __name__ == "__main__":
    unittest.main()
