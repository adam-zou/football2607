import threading
import unittest

from proxy_scheduler import ProxyScheduler, ProxySchedulerError


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


if __name__ == "__main__":
    unittest.main()
