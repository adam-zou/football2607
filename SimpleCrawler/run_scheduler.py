#!/usr/bin/env python3
"""Run the standalone SimpleCrawler jobs on their independent schedules."""

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO

from dotenv import load_dotenv
from simple_crawler.dashboard_statistics import fetch_daily_statistics
from simple_crawler.file_lock import ExclusiveFileLock, FileAlreadyLocked
from simple_crawler.monitoring import DashboardServer, RuntimeMonitor


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_FILE = SCRIPT_DIR / ".env"
DEFAULT_LOCK_FILE = Path(tempfile.gettempdir()) / (
    "football2607-simple-crawler-runtime.lock"
)
PROXY_SCRIPT = SCRIPT_DIR / "proxy_scheduler.py"
PROXY_START_TIMEOUT_SECONDS = 30.0
CHILD_POLL_SECONDS = 0.25
DEFAULT_MONITOR_HOST = "127.0.0.1"
DEFAULT_MONITOR_PORT = 8081
PROXY_MONITOR_INTERVAL_SECONDS = 10.0
DAILY_STATISTICS_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    script: Path
    interval_seconds: float


ProcessRunner = Callable[[WorkerSpec, threading.Event], int]


class SchedulerAlreadyRunning(RuntimeError):
    """Raised when another scheduler process owns the runtime lock."""


class SchedulerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock: Optional[ExclusiveFileLock] = None
        self._file: Optional[TextIO] = None

    def __enter__(self) -> "SchedulerLock":
        lock = ExclusiveFileLock(self.path, timeout=0)
        try:
            lock_file = lock.acquire()
        except FileAlreadyLocked as error:
            raise SchedulerAlreadyRunning(
                "SimpleCrawler 调度器已经在运行"
            ) from error
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()))
            lock_file.flush()
        except Exception:
            lock.release()
            raise
        self._lock = lock
        self._file = lock_file
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._lock is None:
            return
        self._lock.release()
        self._lock = None
        self._file = None


def positive_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} 必须是数字") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def monitor_address() -> tuple[str, int]:
    host = os.environ.get("SIMPLE_CRAWLER_MONITOR_HOST", DEFAULT_MONITOR_HOST).strip()
    if not host:
        raise ValueError("SIMPLE_CRAWLER_MONITOR_HOST 不能为空")
    raw_port = os.environ.get("SIMPLE_CRAWLER_MONITOR_PORT", str(DEFAULT_MONITOR_PORT))
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("SIMPLE_CRAWLER_MONITOR_PORT 必须是整数") from error
    if port < 0 or port > 65535:
        raise ValueError("SIMPLE_CRAWLER_MONITOR_PORT 必须在 0 到 65535 之间")
    return host, port


def worker_specs() -> tuple[WorkerSpec, ...]:
    return (
        WorkerSpec(
            "比赛 ID",
            SCRIPT_DIR / "fetch_match_ids.py",
            positive_env_float("SIMPLE_CRAWLER_ID_INTERVAL_SECONDS", 900.0),
        ),
        WorkerSpec(
            "比赛详情",
            SCRIPT_DIR / "fetch_match_details.py",
            positive_env_float(
                "SIMPLE_CRAWLER_DETAIL_INTERVAL_SECONDS",
                5.0,
            ),
        ),
        WorkerSpec(
            "赔率",
            SCRIPT_DIR / "fetch_odds_pages.py",
            positive_env_float(
                "SIMPLE_CRAWLER_ODDS_INTERVAL_SECONDS",
                5.0,
            ),
        ),
        WorkerSpec(
            "完成核验",
            SCRIPT_DIR / "check_match_completion.py",
            positive_env_float(
                "SIMPLE_CRAWLER_COMPLETION_INTERVAL_SECONDS",
                60.0,
            ),
        ),
    )


def child_environment() -> dict[str, str]:
    return os.environ.copy()


def run_child_process(
    command: Sequence[str],
    stop_event: threading.Event,
    log_callback: Optional[Callable[[str], None]] = None,
) -> int:
    process = subprocess.Popen(
        list(command),
        cwd=PROJECT_ROOT,
        env=child_environment(),
        stdout=subprocess.PIPE if log_callback else None,
        stderr=subprocess.STDOUT if log_callback else None,
        text=True if log_callback else None,
        bufsize=1 if log_callback else -1,
    )
    reader = None
    if log_callback is not None and process.stdout is not None:
        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                log_callback(line.rstrip("\r\n"))

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
    while process.poll() is None:
        if stop_event.wait(CHILD_POLL_SECONDS):
            process.terminate()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
            if reader is not None:
                reader.join(timeout=1)
            return returncode
    if reader is not None:
        reader.join(timeout=1)
    return int(process.returncode or 0)


def run_worker_process(
    spec: WorkerSpec,
    stop_event: threading.Event,
    monitor: Optional[RuntimeMonitor] = None,
) -> int:
    def handle_log(line: str) -> None:
        print(line, flush=True)
        if monitor is not None:
            monitor.append_log(spec.script.stem, line)

    return run_child_process(
        [sys.executable, "-u", str(spec.script)],
        stop_event,
        handle_log if monitor is not None else None,
    )


def run_worker_loop(
    spec: WorkerSpec,
    stop_event: threading.Event,
    runner: ProcessRunner = run_worker_process,
    monitor: Optional[RuntimeMonitor] = None,
) -> None:
    """Run one job at a time, then wait its configured post-round interval."""

    while not stop_event.is_set():
        line = "开始新一轮"
        print(f"[{spec.name}] {line}", flush=True)
        if monitor is not None:
            monitor.append_log(spec.script.stem, line)
            monitor.update(
                spec.script.stem,
                status="running",
                message="脚本正在运行",
                started_at=time.time(),
                next_run_at=None,
            )
        started_at = time.monotonic()
        returncode = None
        try:
            returncode = runner(spec, stop_event)
            outcome = f"退出码 {returncode}"
        except Exception as error:
            outcome = f"启动或执行失败：{error}"
        elapsed = time.monotonic() - started_at
        line = (
            f"本轮结束：{outcome}，耗时 {elapsed:.1f} 秒；"
            f"{spec.interval_seconds:g} 秒后重试"
        )
        print(f"[{spec.name}] {line}", flush=True)
        if monitor is not None:
            now = time.time()
            monitor.append_log(spec.script.stem, line)
            succeeded = returncode == 0
            monitor.update(
                spec.script.stem,
                status="waiting" if succeeded else "error",
                message="等待下一轮" if succeeded else outcome,
                finished_at=now,
                next_run_at=now + spec.interval_seconds,
                duration_seconds=elapsed,
                exit_code=returncode,
            )
        if stop_event.wait(spec.interval_seconds):
            break
    if monitor is not None:
        monitor.update(
            spec.script.stem,
            status="stopped",
            message="调度器已停止",
            next_run_at=None,
        )


def proxy_service_url() -> str:
    return os.environ.get(
        "PROXY_SCHEDULER_URL",
        "http://127.0.0.1:8765",
    ).rstrip("/")


def proxy_is_healthy(timeout_seconds: float = 1.0) -> bool:
    request = urllib.request.Request(proxy_service_url() + "/health")
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds,
        ) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def fetch_proxy_health(timeout_seconds: float = 2.0) -> dict[str, object]:
    request = urllib.request.Request(proxy_service_url() + "/health")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("代理健康接口返回了无效 JSON")
    return payload


def format_proxy_health(payload: dict[str, object]) -> str:
    return (
        f"代理健康：当前代理 {payload.get('pool_size', 0)} 个，"
        f"已租用 {payload.get('leased', 0)} 个，"
        f"可用代理 {payload.get('available_proxies', 0)} 个，"
        f"隔离代理 {payload.get('retired_proxies', 0)} 个，"
        f"可用页面槽位 {payload.get('available_page_slots', 0)}；"
        f"最近获取 {payload.get('last_batch_received', 0)} 个，"
        f"验证通过 {payload.get('last_batch_validated', 0)} 个"
    )


def run_proxy_health_monitor(
    stop_event: threading.Event,
    monitor: RuntimeMonitor,
    interval_seconds: float = PROXY_MONITOR_INTERVAL_SECONDS,
    fetcher: Callable[[], dict[str, object]] = fetch_proxy_health,
) -> None:
    """Append one proxy-pool health summary every configured interval."""

    while not stop_event.wait(interval_seconds):
        try:
            payload = fetcher()
            monitor.update_proxy_health(payload)
            monitor.append_log("proxy_scheduler", format_proxy_health(payload))
            monitor.update(
                "proxy_scheduler",
                status="running",
                message="代理服务健康",
            )
        except Exception as error:
            monitor.set_proxy_health_error(str(error))
            monitor.append_log("proxy_scheduler", f"代理健康检查失败：{error}")
            monitor.update(
                "proxy_scheduler",
                status="error",
                message=f"代理健康检查失败：{error}",
            )


def run_daily_statistics_monitor(
    stop_event: threading.Event,
    monitor: RuntimeMonitor,
    database_url: str,
    interval_seconds: float = DAILY_STATISTICS_INTERVAL_SECONDS,
    fetcher: Callable[[str], dict[str, object]] = fetch_daily_statistics,
) -> None:
    """Refresh dashboard database statistics without blocking HTTP requests."""

    while not stop_event.is_set():
        try:
            monitor.update_daily_statistics(fetcher(database_url))
        except Exception as error:
            monitor.set_daily_statistics_error(str(error))
        if stop_event.wait(interval_seconds):
            break


def ensure_proxy_service(
    stop_event: threading.Event,
    monitor: Optional[RuntimeMonitor] = None,
) -> Optional[subprocess.Popen]:
    if proxy_is_healthy():
        print("[代理服务] 使用已经运行的统一代理服务", flush=True)
        if monitor is not None:
            monitor.append_log("proxy_scheduler", "使用已经运行的统一代理服务")
            monitor.update(
                "proxy_scheduler",
                status="running",
                message="外部代理服务健康",
                started_at=time.time(),
            )
        return None

    process = subprocess.Popen(
        [sys.executable, "-u", str(PROXY_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=child_environment(),
        stdout=subprocess.PIPE if monitor is not None else None,
        stderr=subprocess.STDOUT if monitor is not None else None,
        text=True if monitor is not None else None,
        bufsize=1 if monitor is not None else -1,
    )
    if monitor is not None:
        monitor.update(
            "proxy_scheduler",
            status="starting",
            message="正在启动代理服务",
            started_at=time.time(),
        )

        def read_proxy_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                clean_line = line.rstrip("\r\n")
                print(clean_line, flush=True)
                monitor.append_log("proxy_scheduler", clean_line)

        threading.Thread(target=read_proxy_output, daemon=True).start()
    deadline = time.monotonic() + PROXY_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline and not stop_event.is_set():
        if process.poll() is not None:
            raise RuntimeError(
                f"代理服务启动失败，退出码 {process.returncode}"
            )
        if proxy_is_healthy():
            print("[代理服务] 已启动", flush=True)
            if monitor is not None:
                monitor.append_log("proxy_scheduler", "代理服务已启动并通过健康检查")
                monitor.update(
                    "proxy_scheduler",
                    status="running",
                    message="代理服务健康",
                )
            return process
        stop_event.wait(0.25)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("等待代理服务启动超时")


def stop_process(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum, frame) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def main() -> int:
    load_dotenv(ENV_FILE)
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    lock_path = Path(
        os.environ.get("SIMPLE_CRAWLER_SCHEDULER_LOCK_FILE", DEFAULT_LOCK_FILE)
    )

    try:
        specs = worker_specs()
        monitor_host, monitor_port = monitor_address()
        with SchedulerLock(lock_path):
            monitor = RuntimeMonitor()
            dashboard = None
            proxy_process = None
            threads = []
            proxy_monitor_thread = None
            statistics_monitor_thread = None
            try:
                if monitor_port != 0:
                    dashboard = DashboardServer(monitor, monitor_host, monitor_port)
                    dashboard.start()
                    print(
                        f"[监控页面] http://{monitor_host}:{dashboard.address[1]}/",
                        flush=True,
                    )
                    statistics_monitor_thread = threading.Thread(
                        target=run_daily_statistics_monitor,
                        args=(
                            stop_event,
                            monitor,
                            os.environ.get("SIMPLE_CRAWLER_DATABASE_URL", ""),
                        ),
                        name="simple-crawler-daily-statistics-monitor",
                    )
                    statistics_monitor_thread.start()
                proxy_process = ensure_proxy_service(stop_event, monitor)
                proxy_monitor_thread = threading.Thread(
                    target=run_proxy_health_monitor,
                    args=(stop_event, monitor),
                    name="simple-crawler-proxy-health-monitor",
                )
                proxy_monitor_thread.start()

                def monitored_runner(
                    spec: WorkerSpec,
                    event: threading.Event,
                ) -> int:
                    return run_worker_process(spec, event, monitor)

                threads = [
                    threading.Thread(
                        target=run_worker_loop,
                        args=(spec, stop_event, monitored_runner, monitor),
                        name=f"simple-crawler-{spec.script.stem}",
                    )
                    for spec in specs
                ]
                for thread in threads:
                    thread.start()

                while not stop_event.wait(1.0):
                    if (
                        proxy_process is not None
                        and proxy_process.poll() is not None
                    ):
                        raise RuntimeError(
                            "调度器启动的代理服务意外退出："
                            f"{proxy_process.returncode}"
                        )
            finally:
                stop_event.set()
                for thread in threads:
                    thread.join()
                if proxy_monitor_thread is not None:
                    proxy_monitor_thread.join()
                if statistics_monitor_thread is not None:
                    statistics_monitor_thread.join()
                stop_process(proxy_process)
                monitor.update(
                    "proxy_scheduler",
                    status="stopped",
                    message="代理服务已停止",
                )
                if dashboard is not None:
                    dashboard.close()
    except SchedulerAlreadyRunning as error:
        print(f"[调度器] {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"[调度器] SimpleCrawler 调度器退出：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
