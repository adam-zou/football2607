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
    run_daily_statistics_monitor,
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


class DailyStatisticsMonitorTests(unittest.TestCase):
    def test_updates_monitor_from_database_fetcher(self) -> None:
        stop_event = threading.Event()
        monitor = RuntimeMonitor()

        def fetcher(database_url):
            self.assertEqual(database_url, "postgresql://example")
            stop_event.set()
            return {
                "date": "2026-07-17",
                "match_count": 12,
                "not_started_count": 2,
                "finished_count": 7,
                "in_progress_count": 2,
                "postponed_count": 1,
                "cancelled_count": 0,
                "pending_count": 0,
                "other_status_count": 0,
                "crawl_unfinished_count": 6,
                "crawl_completed_count": 6,
                "abnormal_count": 1,
                "paused_count": 2,
                "finished_unfinished_count": 3,
                "historical_match_count": 230,
                "historical_not_started_count": 0,
                "historical_in_progress_count": 0,
                "historical_finished_count": 220,
                "historical_postponed_count": 3,
                "historical_cancelled_count": 2,
                "historical_pending_count": 1,
                "historical_other_status_count": 4,
                "historical_unfinished_count": 8,
                "historical_completed_count": 210,
                "historical_paused_count": 10,
                "historical_abnormal_count": 2,
                "historical_finished_unfinished_count": 7,
                "missing_details_count": 76,
                "invalid_scheduled_time_count": 3,
                "odds_counts": [],
            }

        run_daily_statistics_monitor(
            stop_event,
            monitor,
            "postgresql://example",
            0.001,
            fetcher,
        )

        statistics = monitor.snapshot()["daily_statistics"]
        self.assertEqual(statistics["match_count"], 12)
        self.assertIsNone(statistics["error"])


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
        self.assertIn("比赛数据统计", html)
        self.assertIn("历史比赛", html)
        self.assertIn("时间异常", html)
        self.assertIn("待获取详情", html)
        self.assertIn("完场但爬取未完成", html)
        self.assertIn("c.logs.join('\\n')", html)
        self.assertIn("抓取到 12 场比赛", payload)
        self.assertIn('"daily_statistics"', payload)


if __name__ == "__main__":
    unittest.main()
