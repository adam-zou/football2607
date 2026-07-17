import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from proxy_scheduler import ProxyClient, ProxyScheduler, ProxySchedulerError


class GlobalApiRateLimitTests(unittest.TestCase):
    def test_waits_and_records_timestamp_while_holding_process_lock(self) -> None:
        scheduler = ProxyScheduler(
            api_url="https://proxy.example.test",
            username="user",
            password="password",
            api_min_interval_seconds=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy-api.lock"
            path.write_text("99", encoding="utf-8")
            with (
                patch("proxy_scheduler.GLOBAL_RATE_LIMIT_FILE", path),
                patch("proxy_scheduler.time.time", side_effect=[100, 101]),
                patch("proxy_scheduler.time.sleep") as sleep,
            ):
                scheduler._wait_for_global_api_slot()

            sleep.assert_called_once_with(1)
            self.assertEqual(path.read_text(encoding="utf-8"), "101")


class FiveUseProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = ProxyScheduler(
            api_url="https://proxy.example.test",
            username="user",
            password="password",
            refresh_seconds=2,
            ttl_seconds=30,
            acquire_timeout_seconds=0.01,
            api_min_interval_seconds=0,
            fetcher=lambda _url, _timeout: "192.0.2.10:8000",
            validator=lambda _proxy, _url, _timeout: True,
        )
        self.scheduler._thread = threading.Thread()
        self.scheduler._refresh_pool()

    def tearDown(self) -> None:
        self.scheduler._thread = None

    def test_proxy_can_hold_five_concurrent_page_leases(self) -> None:
        proxies = [self.scheduler.acquire() for _ in range(5)]

        self.assertEqual(
            {proxy.server for proxy in proxies},
            {"http://192.0.2.10:8000"},
        )
        self.assertEqual(self.scheduler.leased_count, 5)
        self.assertEqual(self.scheduler.available_page_slots, 0)

        with self.assertRaises(ProxySchedulerError):
            self.scheduler.acquire()

        for proxy in proxies:
            self.scheduler.release(proxy, failed=False)

        self.assertEqual(self.scheduler.pool_size, 0)

    def test_five_assignments_permanently_retire_proxy(self) -> None:
        for _ in range(5):
            proxy = self.scheduler.acquire()
            self.scheduler.release(proxy, failed=False)

        self.assertEqual(self.scheduler.pool_size, 0)
        self.scheduler._refresh_pool()

        self.assertEqual(self.scheduler.pool_size, 0)

    def test_page_failure_retires_proxy_before_five_assignments(self) -> None:
        proxy = self.scheduler.acquire()

        self.scheduler.release(proxy, failed=True)
        self.scheduler._refresh_pool()

        self.assertEqual(self.scheduler.pool_size, 0)

    def test_configurable_assignment_limit_is_used(self) -> None:
        scheduler = ProxyScheduler(
            api_url="https://proxy.example.test",
            username="user",
            password="password",
            refresh_seconds=2,
            ttl_seconds=30,
            acquire_timeout_seconds=0.01,
            api_min_interval_seconds=0,
            max_page_assignments_per_proxy=2,
            fetcher=lambda _url, _timeout: "192.0.2.20:8000",
            validator=lambda _proxy, _url, _timeout: True,
        )
        scheduler._thread = threading.Thread()
        scheduler._refresh_pool()

        for _ in range(2):
            proxy = scheduler.acquire()
            scheduler.release(proxy, failed=False)

        self.assertEqual(scheduler.pool_size, 0)
        self.assertEqual(
            scheduler.max_page_assignments_per_proxy,
            2,
        )

    def test_assignment_limit_must_be_positive(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "PROXY_MAX_PAGE_ASSIGNMENTS_PER_IP 必须大于 0",
        ):
            ProxyScheduler(
                api_url="https://proxy.example.test",
                username="user",
                password="password",
                max_page_assignments_per_proxy=0,
            )

    def test_company_lease_reserves_each_market_page_assignment(self) -> None:
        proxy = self.scheduler.acquire(page_assignments=3)

        self.assertEqual(self.scheduler.available_page_slots, 2)
        with self.assertRaises(ProxySchedulerError):
            self.scheduler.acquire(page_assignments=3)

        self.scheduler.release(proxy, failed=False)

    def test_page_assignment_request_must_fit_per_proxy_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "页面分配数量"):
            self.scheduler.acquire(page_assignments=6)

    def test_assignment_limit_is_loaded_from_environment(self) -> None:
        environment = {
            "PROXY_API_URL": "https://proxy.example.test",
            "PROXY_USERNAME": "user",
            "PROXY_PASSWORD": "password",
            "PROXY_MAX_PAGE_ASSIGNMENTS_PER_IP": "7",
        }
        with patch.dict("os.environ", environment, clear=True):
            scheduler = ProxyScheduler.from_env()

        self.assertEqual(scheduler.max_page_assignments_per_proxy, 7)


class ProxyClientLeaseTests(unittest.TestCase):
    def test_client_sends_company_page_assignment_count(self) -> None:
        client = ProxyClient("http://proxy-service.test")
        responses = [
            {
                "lease_id": "lease-1",
                "server": "http://192.0.2.30:8000",
                "username": "user",
                "password": "password",
            },
            {"released": True},
        ]

        with patch.object(client, "_post", side_effect=responses) as post:
            with client.lease(page_assignments=3):
                pass

        self.assertEqual(post.call_args_list[0].args[0], "/lease")
        self.assertEqual(
            post.call_args_list[0].args[1]["page_assignments"],
            3,
        )


if __name__ == "__main__":
    unittest.main()
