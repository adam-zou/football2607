import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from run_scheduler import (
    SchedulerAlreadyRunning,
    SchedulerLock,
    WorkerSpec,
    format_proxy_health,
    monitor_address,
    positive_env_float,
    run_proxy_health_monitor,
    run_worker_loop,
    worker_specs,
)
from simple_crawler.monitoring import DashboardServer, RuntimeMonitor


class SchedulerConfigurationTests(unittest.TestCase):
    def test_default_intervals_match_runtime_policy(self) -> None:
        names = (
            "SIMPLE_CRAWLER_ID_INTERVAL_SECONDS",
            "SIMPLE_CRAWLER_DETAIL_INTERVAL_SECONDS",
            "SIMPLE_CRAWLER_ODDS_INTERVAL_SECONDS",
            "SIMPLE_CRAWLER_COMPLETION_INTERVAL_SECONDS",
        )
        with patch.dict(os.environ, {name: "" for name in names}):
            intervals = {
                spec.script.name: spec.interval_seconds
                for spec in worker_specs()
            }

        self.assertEqual(intervals["fetch_match_ids.py"], 60.0)
        self.assertEqual(intervals["fetch_match_details.py"], 5.0)
        self.assertEqual(intervals["fetch_odds_pages.py"], 5.0)
        self.assertEqual(intervals["check_match_completion.py"], 60.0)

    def test_interval_must_be_positive(self) -> None:
        with patch.dict(os.environ, {"TEST_INTERVAL": "0"}):
            with self.assertRaisesRegex(ValueError, "必须大于 0"):
                positive_env_float("TEST_INTERVAL", 1.0)

    def test_interval_must_be_finite(self) -> None:
        with patch.dict(os.environ, {"TEST_INTERVAL": "nan"}):
            with self.assertRaisesRegex(ValueError, "必须大于 0"):
                positive_env_float("TEST_INTERVAL", 1.0)

    def test_monitor_defaults_to_local_port_8081(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SIMPLE_CRAWLER_MONITOR_HOST": "",
                "SIMPLE_CRAWLER_MONITOR_PORT": "",
            },
            clear=False,
        ):
            os.environ.pop("SIMPLE_CRAWLER_MONITOR_HOST", None)
            os.environ.pop("SIMPLE_CRAWLER_MONITOR_PORT", None)
            self.assertEqual(monitor_address(), ("127.0.0.1", 8081))

    def test_monitor_port_zero_disables_http(self) -> None:
        with patch.dict(
            os.environ,
            {"SIMPLE_CRAWLER_MONITOR_PORT": "0"},
        ):
            self.assertEqual(monitor_address()[1], 0)


class WorkerLoopTests(unittest.TestCase):
    def test_worker_runs_sequentially_until_stopped(self) -> None:
        stop_event = threading.Event()
        calls = []
        spec = WorkerSpec("测试", Path("job.py"), 0.001)

        def runner(received_spec, received_stop_event) -> int:
            calls.append(received_spec.script.name)
            if len(calls) == 2:
                received_stop_event.set()
            return 0

        run_worker_loop(spec, stop_event, runner)

        self.assertEqual(calls, ["job.py", "job.py"])

    def test_worker_updates_monitor_and_keeps_bounded_logs(self) -> None:
        stop_event = threading.Event()
        spec = WorkerSpec("测试", Path("fetch_match_ids.py"), 0.001)
        monitor = RuntimeMonitor(max_log_lines=2)

        def runner(received_spec, received_stop_event) -> int:
            del received_spec
            received_stop_event.set()
            return 0

        run_worker_loop(spec, stop_event, runner, monitor)

        component = monitor.snapshot()["components"][1]
        self.assertEqual(component["status"], "stopped")
        self.assertEqual(component["exit_code"], 0)
        self.assertLessEqual(len(component["logs"]), 2)

    def test_worker_retries_after_runner_error(self) -> None:
        stop_event = threading.Event()
        calls = []
        spec = WorkerSpec("测试", Path("job.py"), 0.001)

        def runner(received_spec, received_stop_event) -> int:
            calls.append(received_spec.script.name)
            if len(calls) == 1:
                raise RuntimeError("temporary")
            received_stop_event.set()
            return 0

        run_worker_loop(spec, stop_event, runner)

        self.assertEqual(calls, ["job.py", "job.py"])


class ProxyHealthMonitorTests(unittest.TestCase):
    def test_appends_proxy_health_summary(self) -> None:
        stop_event = threading.Event()
        monitor = RuntimeMonitor()

        def fetcher():
            stop_event.set()
            return {
                "pool_size": 8,
                "leased": 2,
                "available_proxies": 6,
                "available_page_slots": 28,
                "last_batch_received": 10,
                "last_batch_validated": 8,
            }

        run_proxy_health_monitor(stop_event, monitor, 0.001, fetcher)

        proxy = monitor.snapshot()["components"][0]
        self.assertEqual(proxy["status"], "running")
        self.assertIn("当前代理 8 个", proxy["logs"][-1])
        self.assertIn("可用页面槽位 28", proxy["logs"][-1])

    def test_formats_missing_health_fields_as_zero(self) -> None:
        self.assertIn("当前代理 0 个", format_proxy_health({}))


@unittest.skipIf(os.name == "nt", "fcntl lock is unavailable on Windows")
class SchedulerLockTests(unittest.TestCase):
    def test_second_scheduler_cannot_acquire_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.lock"
            with SchedulerLock(path):
                with self.assertRaises(SchedulerAlreadyRunning):
                    with SchedulerLock(path):
                        pass


class DashboardServerTests(unittest.TestCase):
    def test_serves_dashboard_and_json_snapshot(self) -> None:
        monitor = RuntimeMonitor()
        monitor.append_log("fetch_match_ids", "抓取到 12 场比赛")
        server = DashboardServer(monitor, "127.0.0.1", 0)
        server.start()
        try:
            host, port = server.address
            with urllib.request.urlopen(f"http://{host}:{port}/") as response:
                html = response.read().decode("utf-8")
            with urllib.request.urlopen(
                f"http://{host}:{port}/api/status"
            ) as response:
                payload = response.read().decode("utf-8")
        finally:
            server.close()

        self.assertIn("SimpleCrawler 总监控", html)
        self.assertIn("c.logs.join('\\n')", html)
        self.assertIn("抓取到 12 场比赛", payload)


if __name__ == "__main__":
    unittest.main()
