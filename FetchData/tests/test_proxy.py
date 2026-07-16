import asyncio
import os
import unittest
from unittest.mock import patch

from fetch_data.observability import RuntimeObservability
from fetch_data.proxy import ProxyError, ProxyManager, ProxySettings


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ProxyManagerTests(unittest.TestCase):
    def test_https_validation_sends_proxy_credentials_on_initial_connect(
        self,
    ) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        class ConnectAuthProxy:
            def open(self, request, timeout):
                if request.get_header("Proxy-authorization") is None:
                    raise OSError("Tunnel connection failed: 407")
                return FakeResponse()

        proxy = ProxySettings(
            server="http://127.0.0.1:8080",
            username="user",
            password="secret",
        )

        with patch(
            "fetch_data.proxy.urllib.request.build_opener",
            return_value=ConnectAuthProxy(),
        ):
            is_valid = ProxyManager._validate_proxy(
                proxy,
                "https://example.com",
                {},
                1,
            )

        self.assertTrue(is_valid)

    def test_plain_text_proxy_is_parsed_for_playwright(self) -> None:
        proxy = ProxyManager.parse_proxy_response(
            "127.0.0.1:8080\n",
            username="user",
            password="secret",
        )

        self.assertEqual(
            proxy.playwright_options(),
            {
                "server": "http://127.0.0.1:8080",
                "username": "user",
                "password": "secret",
            },
        )
        self.assertNotIn("secret", repr(proxy))

    def test_malformed_proxy_responses_are_rejected(self) -> None:
        responses = [
            "",
            "127.0.0.1",
            "127.0.0.1:8080\n127.0.0.2:8080",
            "http://user:secret@127.0.0.1:8080",
        ]

        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(ProxyError):
                    ProxyManager.parse_proxy_response(
                        response,
                        username="user",
                        password="secret",
                    )

    def test_proxy_is_cached_then_refreshed_after_interval(self) -> None:
        responses = iter(["127.0.0.1:8001", "127.0.0.1:8002"])
        calls = []
        clock = FakeClock()

        def fetcher(api_url, headers, timeout):
            calls.append(api_url)
            return next(responses)

        manager = ProxyManager(
            api_url="https://supplier.example/get",
            username="user",
            password="secret",
            update_interval=300,
            fetcher=fetcher,
            validator=lambda proxy, url, headers, timeout: True,
            clock=clock,
        )

        async def exercise() -> None:
            first = await manager.get_proxy()
            clock.now = 299
            cached = await manager.get_proxy()
            clock.now = 300
            refreshed = await manager.get_proxy()

            self.assertIs(first, cached)
            self.assertNotEqual(first.server, refreshed.server)

        asyncio.run(exercise())
        self.assertEqual(len(calls), 2)

    def test_consecutive_errors_force_an_early_refresh(self) -> None:
        responses = iter(["127.0.0.1:8001", "127.0.0.1:8002"])
        manager = ProxyManager(
            api_url="https://supplier.example/get",
            username="user",
            password="secret",
            update_interval=300,
            max_consecutive_errors=2,
            fetcher=lambda api_url, headers, timeout: next(responses),
            validator=lambda proxy, url, headers, timeout: True,
            clock=FakeClock(),
        )

        async def exercise() -> None:
            first = await manager.get_proxy()
            await manager.report_error()
            self.assertIs(first, await manager.get_proxy())
            await manager.report_error()
            refreshed = await manager.get_proxy()
            self.assertNotEqual(first.server, refreshed.server)

        asyncio.run(exercise())

    def test_force_refresh_replaces_and_validates_cached_proxy(self) -> None:
        responses = iter(["127.0.0.1:8001", "127.0.0.1:8002"])
        validated = []
        manager = ProxyManager(
            api_url="https://supplier.example/get",
            username="user",
            password="secret",
            update_interval=300,
            fetcher=lambda api_url, headers, timeout: next(responses),
            validator=lambda proxy, url, headers, timeout: (
                validated.append(proxy.server) or True
            ),
            clock=FakeClock(),
        )

        async def exercise() -> None:
            first = await manager.get_proxy()
            refreshed = await manager.force_refresh()
            self.assertNotEqual(first.server, refreshed.server)

        asyncio.run(exercise())
        self.assertEqual(len(validated), 2)

    def test_proxy_refresh_validation_and_invalidation_are_observable(self) -> None:
        observability = RuntimeObservability()
        responses = iter(["127.0.0.1:8001", "127.0.0.1:8002"])
        manager = ProxyManager(
            api_url="https://supplier.example/get",
            username="user",
            password="secret",
            max_consecutive_errors=1,
            fetcher=lambda api_url, headers, timeout: next(responses),
            validator=lambda proxy, url, headers, timeout: True,
            clock=FakeClock(),
            observability=observability,
        )

        async def exercise() -> None:
            await manager.get_proxy()
            await manager.report_error()
            await manager.get_proxy()

        asyncio.run(exercise())
        metrics = observability.render_metrics()
        self.assertIn(
            'football_proxy_requests_total{result="refresh"} 2', metrics
        )
        self.assertIn(
            'football_proxy_validation_total{result="success"} 2', metrics
        )
        self.assertIn("football_proxy_invalidations_total 1", metrics)

    def test_success_resets_consecutive_errors(self) -> None:
        responses = iter(["127.0.0.1:8001", "127.0.0.1:8002"])
        manager = ProxyManager(
            api_url="https://supplier.example/get",
            username="user",
            password="secret",
            max_consecutive_errors=2,
            fetcher=lambda api_url, headers, timeout: next(responses),
            validator=lambda proxy, url, headers, timeout: True,
            clock=FakeClock(),
        )

        async def exercise() -> None:
            first = await manager.get_proxy()
            await manager.report_error()
            await manager.report_success()
            await manager.report_error()
            self.assertIs(first, await manager.get_proxy())

        asyncio.run(exercise())

    def test_environment_configuration_is_required(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                ValueError,
                "PROXY_API_URL, PROXY_USERNAME, PROXY_PASSWORD",
            ):
                ProxyManager.from_env()

    def test_environment_configuration_uses_non_secret_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PROXY_API_URL": "https://supplier.example/get",
                "PROXY_USERNAME": "user",
                "PROXY_PASSWORD": "secret",
            },
            clear=True,
        ):
            manager = ProxyManager.from_env()

        self.assertEqual(manager.update_interval, 60)
        self.assertEqual(manager.max_consecutive_errors, 3)
        self.assertEqual(manager.api_timeout, 5)
        self.assertEqual(manager.test_timeout, 5)


if __name__ == "__main__":
    unittest.main()
