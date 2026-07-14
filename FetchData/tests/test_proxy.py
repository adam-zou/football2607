import asyncio
import os
import unittest
from unittest.mock import patch

from fetch_data.proxy import ProxyError, ProxyManager


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ProxyManagerTests(unittest.TestCase):
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

        self.assertEqual(manager.update_interval, 300)
        self.assertEqual(manager.max_consecutive_errors, 3)
        self.assertEqual(manager.api_timeout, 5)
        self.assertEqual(manager.test_timeout, 5)


if __name__ == "__main__":
    unittest.main()
