#!/usr/bin/env python3
"""Shared short-lived proxy pool used by every SimpleCrawler page request."""

import os
import json
import re
import base64
import threading
import time
import tempfile
import uuid
import urllib.error
import urllib.request
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional

from dotenv import load_dotenv
from simple_crawler.file_lock import ExclusiveFileLock


ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}
PROXY_PATTERN = re.compile(
    r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3}):(\d{1,5})(?!\d)"
)
GLOBAL_RATE_LIMIT_FILE = Path(tempfile.gettempdir()) / (
    "football2607-simple-crawler-proxy-api.lock"
)
DEFAULT_MAX_PAGE_ASSIGNMENTS_PER_PROXY = 5
DEFAULT_PROXY_RETIRE_SECONDS = 3600.0


class ProxySchedulerError(RuntimeError):
    """Raised when no usable proxy can be assigned."""


@dataclass
class ProxyEndpoint:
    server: str
    username: str
    password: str = field(repr=False)
    fetched_at: float
    expires_at: float

    def playwright_options(self) -> Dict[str, str]:
        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }


ProxyFetcher = Callable[[str, float], str]
ProxyValidator = Callable[[ProxyEndpoint, str, float], bool]


class ProxyScheduler:
    """Refresh a proxy pool and limit page assignments per address."""

    def __init__(
        self,
        *,
        api_url: str,
        username: str,
        password: str,
        refresh_seconds: float = 1.6,
        ttl_seconds: float = 30.0,
        api_timeout_seconds: float = 5.0,
        acquire_timeout_seconds: float = 5.0,
        api_min_interval_seconds: float = 1.6,
        fetcher: Optional[ProxyFetcher] = None,
        test_url: str = "https://live.titan007.com/oldIndexall.aspx",
        test_timeout_seconds: float = 5.0,
        validation_workers: int = 10,
        max_page_assignments_per_proxy: int = (
            DEFAULT_MAX_PAGE_ASSIGNMENTS_PER_PROXY
        ),
        retirement_seconds: float = DEFAULT_PROXY_RETIRE_SECONDS,
        validator: Optional[ProxyValidator] = None,
    ) -> None:
        if not api_url.strip():
            raise ValueError("PROXY_API_URL 不能为空")
        if not username.strip():
            raise ValueError("PROXY_USERNAME 不能为空")
        if not password:
            raise ValueError("PROXY_PASSWORD 不能为空")
        if refresh_seconds <= 0:
            raise ValueError("PROXY_REFRESH_SECONDS 必须大于 0")
        if ttl_seconds <= refresh_seconds:
            raise ValueError("PROXY_TTL_SECONDS 必须大于刷新间隔")
        if api_timeout_seconds <= 0 or acquire_timeout_seconds <= 0:
            raise ValueError("代理超时必须大于 0")
        if api_min_interval_seconds < 0:
            raise ValueError("PROXY_API_MIN_INTERVAL_SECONDS 不能小于 0")
        if not test_url.strip():
            raise ValueError("PROXY_TEST_URL 不能为空")
        if test_timeout_seconds <= 0:
            raise ValueError("PROXY_TEST_TIMEOUT_SECONDS 必须大于 0")
        if validation_workers <= 0:
            raise ValueError("PROXY_VALIDATION_WORKERS 必须大于 0")
        if max_page_assignments_per_proxy <= 0:
            raise ValueError(
                "PROXY_MAX_PAGE_ASSIGNMENTS_PER_IP 必须大于 0"
            )
        if retirement_seconds <= 0:
            raise ValueError("PROXY_RETIRE_SECONDS 必须大于 0")

        self.api_url = api_url
        self.username = username.strip()
        self.password = password
        self.refresh_seconds = refresh_seconds
        self.ttl_seconds = ttl_seconds
        self.api_timeout_seconds = api_timeout_seconds
        self.acquire_timeout_seconds = acquire_timeout_seconds
        self.api_min_interval_seconds = api_min_interval_seconds
        self._fetcher = fetcher or self._fetch_proxy_text
        self.test_url = test_url
        self.test_timeout_seconds = test_timeout_seconds
        self.validation_workers = validation_workers
        self.max_page_assignments_per_proxy = max_page_assignments_per_proxy
        self.retirement_seconds = retirement_seconds
        self._validator = validator or self._validate_proxy
        self._condition = threading.Condition()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proxies: Dict[str, ProxyEndpoint] = {}
        self._leased: Dict[str, int] = {}
        self._assignment_counts: Dict[str, int] = {}
        self._retired_until: Dict[str, float] = {}
        self._last_error: Optional[Exception] = None
        self.last_batch_received = 0
        self.last_batch_validated = 0

    @classmethod
    def from_env(cls) -> "ProxyScheduler":
        load_dotenv(ENV_FILE)
        required = {
            "PROXY_API_URL": os.environ.get("PROXY_API_URL", "").strip(),
            "PROXY_USERNAME": os.environ.get("PROXY_USERNAME", "").strip(),
            "PROXY_PASSWORD": os.environ.get("PROXY_PASSWORD", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("缺少代理配置：" + ", ".join(missing))
        return cls(
            api_url=required["PROXY_API_URL"],
            username=required["PROXY_USERNAME"],
            password=required["PROXY_PASSWORD"],
            refresh_seconds=cls._float_env("PROXY_REFRESH_SECONDS", 1.6),
            ttl_seconds=cls._float_env("PROXY_TTL_SECONDS", 30.0),
            api_timeout_seconds=cls._float_env(
                "PROXY_API_TIMEOUT_SECONDS",
                5.0,
            ),
            acquire_timeout_seconds=cls._float_env(
                "PROXY_ACQUIRE_TIMEOUT_SECONDS",
                5.0,
            ),
            api_min_interval_seconds=cls._float_env(
                "PROXY_API_MIN_INTERVAL_SECONDS",
                1.6,
            ),
            test_url=os.environ.get(
                "PROXY_TEST_URL",
                "https://live.titan007.com/oldIndexall.aspx",
            ),
            test_timeout_seconds=cls._float_env(
                "PROXY_TEST_TIMEOUT_SECONDS",
                5.0,
            ),
            validation_workers=cls._int_env(
                "PROXY_VALIDATION_WORKERS",
                10,
            ),
            max_page_assignments_per_proxy=cls._int_env(
                "PROXY_MAX_PAGE_ASSIGNMENTS_PER_IP",
                DEFAULT_MAX_PAGE_ASSIGNMENTS_PER_PROXY,
            ),
            retirement_seconds=cls._float_env(
                "PROXY_RETIRE_SECONDS",
                DEFAULT_PROXY_RETIRE_SECONDS,
            ),
        )

    def __enter__(self) -> "ProxyScheduler":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._refresh_loop,
            name="simple-crawler-proxy-scheduler",
            daemon=True,
        )
        self._thread.start()
        try:
            self._refresh_pool()
        except Exception as error:
            with self._condition:
                self._last_error = error
                self._condition.notify_all()

    def close(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.api_timeout_seconds + 1)
        self._thread = None

    @contextmanager
    def lease(
        self,
        *,
        min_remaining_seconds: float = 1.0,
        page_assignments: int = 1,
    ) -> Iterator[ProxyEndpoint]:
        proxy = self.acquire(
            min_remaining_seconds=min_remaining_seconds,
            page_assignments=page_assignments,
        )
        try:
            yield proxy
        except BaseException:
            self.release(proxy, failed=True)
            raise
        else:
            self.release(proxy, failed=False)

    def acquire(
        self,
        *,
        min_remaining_seconds: float = 1.0,
        page_assignments: int = 1,
    ) -> ProxyEndpoint:
        if self._thread is None:
            raise ProxySchedulerError("代理调度器尚未启动")
        if min_remaining_seconds <= 0:
            raise ValueError("min_remaining_seconds 必须大于 0")
        if min_remaining_seconds >= self.ttl_seconds:
            raise ValueError("页面所需代理时间必须小于代理有效期")
        if page_assignments <= 0:
            raise ValueError("页面分配数量必须大于 0")
        if page_assignments > self.max_page_assignments_per_proxy:
            raise ValueError("页面分配数量不能超过单个代理上限")

        deadline = time.monotonic() + self.acquire_timeout_seconds
        with self._condition:
            while True:
                now = time.monotonic()
                self._remove_expired(now)
                for server, proxy in self._proxies.items():
                    if self._is_retired(server, now):
                        continue
                    assignment_count = self._assignment_counts.get(server, 0)
                    if (
                        assignment_count + page_assignments
                        > self.max_page_assignments_per_proxy
                    ):
                        continue
                    if proxy.expires_at - now < min_remaining_seconds:
                        continue
                    self._leased[server] = self._leased.get(server, 0) + 1
                    assignment_count += page_assignments
                    self._assignment_counts[server] = assignment_count
                    if (
                        assignment_count
                        >= self.max_page_assignments_per_proxy
                    ):
                        self._retire(server, now)
                    return proxy

                remaining = deadline - now
                if remaining <= 0:
                    reason = f"：{self._last_error}" if self._last_error else ""
                    raise ProxySchedulerError("等待可用代理超时" + reason)
                self._condition.wait(
                    timeout=min(remaining, self.refresh_seconds)
                )

    def release(self, proxy: ProxyEndpoint, *, failed: bool) -> None:
        with self._condition:
            active_count = self._leased.get(proxy.server, 0)
            if active_count <= 1:
                self._leased.pop(proxy.server, None)
            else:
                self._leased[proxy.server] = active_count - 1
            if failed:
                self._retire(proxy.server, time.monotonic())
            if (
                self._is_retired(proxy.server, time.monotonic())
                and proxy.server not in self._leased
            ) or proxy.expires_at <= time.monotonic():
                self._proxies.pop(proxy.server, None)
            self._condition.notify_all()

    @property
    def pool_size(self) -> int:
        with self._condition:
            self._remove_expired(time.monotonic())
            return len(self._proxies)

    @property
    def leased_count(self) -> int:
        with self._condition:
            return sum(self._leased.values())

    @property
    def available_proxy_count(self) -> int:
        with self._condition:
            self._remove_expired(time.monotonic())
            return sum(
                1
                for server in self._proxies
                if not self._is_retired(server, time.monotonic())
                and self._assignment_counts.get(server, 0)
                < self.max_page_assignments_per_proxy
            )

    @property
    def available_page_slots(self) -> int:
        with self._condition:
            self._remove_expired(time.monotonic())
            return sum(
                self.max_page_assignments_per_proxy
                - self._assignment_counts.get(server, 0)
                for server in self._proxies
                if not self._is_retired(server, time.monotonic())
            )

    @property
    def retired_proxy_count(self) -> int:
        with self._condition:
            now = time.monotonic()
            self._remove_expired(now)
            return sum(until > now for until in self._retired_until.values())

    def _refresh_loop(self) -> None:
        while not self._stop_event.wait(self.refresh_seconds):
            try:
                self._refresh_pool()
            except Exception as error:
                with self._condition:
                    self._last_error = error
                    self._condition.notify_all()

    def _refresh_pool(self) -> None:
        with self._refresh_lock:
            self._wait_for_global_api_slot()
            fetched_at = time.monotonic()
            text = self._fetcher(self.api_url, self.api_timeout_seconds)
            servers = self.parse_proxy_servers(text)
            if not servers:
                message = "代理 API 没有返回 host:port"
                try:
                    payload = json.loads(text)
                except (TypeError, ValueError):
                    pass
                else:
                    supplier_message = str(payload.get("msg") or "").strip()
                    if supplier_message:
                        message += f"：{supplier_message}"
                raise ProxySchedulerError(message)
            expires_at = fetched_at + self.ttl_seconds
            with self._condition:
                self._remove_expired(time.monotonic())
                retired_servers = set(self._retired_until)
            candidates = [
                ProxyEndpoint(
                    server=server,
                    username=self.username,
                    password=self.password,
                    fetched_at=fetched_at,
                    expires_at=expires_at,
                )
                for server in servers
                if server not in retired_servers
            ]
            if not candidates:
                self.last_batch_received = len(servers)
                self.last_batch_validated = 0
                with self._condition:
                    self._last_error = None
                    self._condition.notify_all()
                return
            worker_count = min(self.validation_workers, len(candidates))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(
                    executor.map(
                        lambda proxy: self._validator(
                            proxy,
                            self.test_url,
                            self.test_timeout_seconds,
                        ),
                        candidates,
                    )
                )
            validated = [
                proxy
                for proxy, is_valid in zip(candidates, results)
                if is_valid and proxy.expires_at > time.monotonic()
            ]
            self.last_batch_received = len(candidates)
            self.last_batch_validated = len(validated)
            if not validated:
                raise ProxySchedulerError(
                    f"本批 {len(candidates)} 个代理全部验证失败"
                )

            with self._condition:
                self._remove_expired(time.monotonic())
                for candidate in validated:
                    if self._is_retired(candidate.server, time.monotonic()):
                        continue
                    existing = self._proxies.get(candidate.server)
                    if existing is not None:
                        existing.fetched_at = fetched_at
                        existing.expires_at = expires_at
                    else:
                        self._proxies[candidate.server] = candidate
                self._last_error = None
                self._condition.notify_all()

    def _wait_for_global_api_slot(self) -> None:
        if self.api_min_interval_seconds == 0:
            return
        lock_timeout = self.api_min_interval_seconds + 5.0
        with ExclusiveFileLock(
            GLOBAL_RATE_LIMIT_FILE,
            timeout=lock_timeout,
        ) as lock_file:
            lock_file.seek(0)
            raw = lock_file.read().strip()
            last_request_at = float(raw) if raw else 0.0
            wait_seconds = (
                self.api_min_interval_seconds
                - (time.time() - last_request_at)
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            request_at = time.time()
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(request_at))
            lock_file.flush()

    def _remove_expired(self, now: float) -> None:
        expired = [
            server
            for server, proxy in self._proxies.items()
            if proxy.expires_at <= now and server not in self._leased
        ]
        for server in expired:
            self._proxies.pop(server, None)
            if server not in self._retired_until:
                self._assignment_counts.pop(server, None)

        completed_retirement_periods = [
            server
            for server, retired_until in self._retired_until.items()
            if retired_until <= now
        ]
        for server in completed_retirement_periods:
            # Quarantine expiry never revives the old endpoint. A subsequent
            # supplier response must create and validate a fresh endpoint.
            self._proxies.pop(server, None)
            if server in self._leased:
                continue
            self._retired_until.pop(server, None)
            self._assignment_counts.pop(server, None)

    def _retire(self, server: str, now: float) -> None:
        retired_until = now + self.retirement_seconds
        self._retired_until[server] = max(
            retired_until,
            self._retired_until.get(server, 0.0),
        )

    def _is_retired(self, server: str, now: float) -> bool:
        return self._retired_until.get(server, 0.0) > now

    @staticmethod
    def parse_proxy_servers(text: str) -> list[str]:
        servers = []
        seen = set()
        for host, port_text in PROXY_PATTERN.findall(text or ""):
            if any(int(part) > 255 for part in host.split(".")):
                continue
            port = int(port_text)
            if port <= 0 or port > 65535:
                continue
            server = f"http://{host}:{port}"
            if server not in seen:
                seen.add(server)
                servers.append(server)
        return servers

    @staticmethod
    def _fetch_proxy_text(url: str, timeout: float) -> str:
        request = urllib.request.Request(url, headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    @staticmethod
    def _validate_proxy(
        proxy: ProxyEndpoint,
        test_url: str,
        timeout: float,
    ) -> bool:
        password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(
            None,
            proxy.server,
            proxy.username,
            proxy.password,
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": proxy.server, "https": proxy.server}
            ),
            urllib.request.ProxyBasicAuthHandler(password_manager),
        )
        request = urllib.request.Request(test_url, headers=DEFAULT_HEADERS)
        credentials = base64.b64encode(
            f"{proxy.username}:{proxy.password}".encode("utf-8")
        ).decode("ascii")
        request.add_unredirected_header(
            "Proxy-Authorization",
            f"Basic {credentials}",
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                return 200 <= response.status < 400
        except Exception:
            return False

    @staticmethod
    def _float_env(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return float(raw)
        except ValueError as error:
            raise ValueError(f"{name} 必须是数字") from error

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError as error:
            raise ValueError(f"{name} 必须是整数") from error


class ProxyLeaseService:
    """Expose the one scheduler's leases to every crawler process."""

    def __init__(self, scheduler: ProxyScheduler) -> None:
        self.scheduler = scheduler
        self._lock = threading.Lock()
        self._leases: Dict[str, ProxyEndpoint] = {}

    def acquire(
        self,
        min_remaining_seconds: float,
        page_assignments: int = 1,
    ) -> Dict[str, object]:
        self._reap_expired_leases()
        proxy = self.scheduler.acquire(
            min_remaining_seconds=min_remaining_seconds,
            page_assignments=page_assignments,
        )
        lease_id = uuid.uuid4().hex
        with self._lock:
            self._leases[lease_id] = proxy
        return {
            "lease_id": lease_id,
            "server": proxy.server,
            "username": proxy.username,
            "password": proxy.password,
            "expires_in": max(0.0, proxy.expires_at - time.monotonic()),
        }

    def release(self, lease_id: str, failed: bool) -> bool:
        with self._lock:
            proxy = self._leases.pop(lease_id, None)
        if proxy is None:
            return False
        self.scheduler.release(proxy, failed=failed)
        return True

    def health(self) -> Dict[str, object]:
        self._reap_expired_leases()
        with self._lock:
            active_leases = len(self._leases)
        return {
            "status": "ok",
            "pool_size": self.scheduler.pool_size,
            "leased": active_leases,
            "available_proxies": self.scheduler.available_proxy_count,
            "available_page_slots": self.scheduler.available_page_slots,
            "retired_proxies": self.scheduler.retired_proxy_count,
            "max_pages_per_proxy": (
                self.scheduler.max_page_assignments_per_proxy
            ),
            "last_batch_received": self.scheduler.last_batch_received,
            "last_batch_validated": self.scheduler.last_batch_validated,
        }

    def _reap_expired_leases(self) -> None:
        now = time.monotonic()
        expired = []
        with self._lock:
            for lease_id, proxy in list(self._leases.items()):
                if proxy.expires_at <= now:
                    expired.append(self._leases.pop(lease_id))
        for proxy in expired:
            self.scheduler.release(proxy, failed=True)


class ProxySchedulerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: ProxyLeaseService) -> None:
        super().__init__(address, ProxySchedulerRequestHandler)
        self.service = service


class ProxySchedulerRequestHandler(BaseHTTPRequestHandler):
    server: ProxySchedulerHTTPServer

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, self.server.service.health())
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/lease":
                minimum = float(payload.get("min_remaining_seconds", 1.0))
                page_assignments = int(payload.get("page_assignments", 1))
                self._send_json(
                    200,
                    self.server.service.acquire(
                        minimum,
                        page_assignments,
                    ),
                )
                return
            if self.path == "/release":
                lease_id = str(payload.get("lease_id") or "")
                if not lease_id:
                    raise ValueError("lease_id 不能为空")
                released = self.server.service.release(
                    lease_id,
                    bool(payload.get("failed", False)),
                )
                self._send_json(200, {"released": released})
                return
            self._send_json(404, {"error": "not found"})
        except (ProxySchedulerError, ValueError) as error:
            self._send_json(503, {"error": str(error)})
        except Exception as error:
            self._send_json(500, {"error": str(error)})

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json(self) -> Dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _send_json(self, status: int, payload: Dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@dataclass(frozen=True)
class RemoteProxyEndpoint:
    lease_id: str
    server: str
    username: str
    password: str = field(repr=False)

    def playwright_options(self) -> Dict[str, str]:
        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }


class ProxyClient:
    """Client used by crawlers; it never calls the supplier API itself."""

    def __init__(
        self,
        service_url: str,
        *,
        ttl_seconds: float = 30.0,
        request_timeout_seconds: float = 20.0,
    ) -> None:
        if not service_url.strip():
            raise ValueError("PROXY_SCHEDULER_URL 不能为空")
        self.service_url = service_url.rstrip("/")
        self.ttl_seconds = ttl_seconds
        self.request_timeout_seconds = request_timeout_seconds

    def __enter__(self) -> "ProxyClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    @classmethod
    def from_env(cls) -> "ProxyClient":
        load_dotenv(ENV_FILE)
        return cls(
            os.environ.get(
                "PROXY_SCHEDULER_URL",
                "http://127.0.0.1:8765",
            ),
            ttl_seconds=ProxyScheduler._float_env(
                "PROXY_TTL_SECONDS",
                30.0,
            ),
            request_timeout_seconds=ProxyScheduler._float_env(
                "PROXY_CLIENT_TIMEOUT_SECONDS",
                20.0,
            ),
        )

    @contextmanager
    def lease(
        self,
        *,
        min_remaining_seconds: float = 1.0,
        page_assignments: int = 1,
    ) -> Iterator[RemoteProxyEndpoint]:
        payload = self._post(
            "/lease",
            {
                "min_remaining_seconds": min_remaining_seconds,
                "page_assignments": page_assignments,
            },
        )
        proxy = RemoteProxyEndpoint(
            lease_id=str(payload["lease_id"]),
            server=str(payload["server"]),
            username=str(payload["username"]),
            password=str(payload["password"]),
        )
        try:
            yield proxy
        except BaseException:
            try:
                self._release(proxy.lease_id, failed=True)
            except Exception:
                pass
            raise
        else:
            self._release(proxy.lease_id, failed=False)

    def health(self) -> Dict[str, object]:
        request = urllib.request.Request(self.service_url + "/health")
        return self._request_json(request)

    def _release(self, lease_id: str, failed: bool) -> None:
        self._post(
            "/release",
            {"lease_id": lease_id, "failed": failed},
        )

    def _post(self, path: str, payload: Dict[str, object]) -> Dict[str, object]:
        request = urllib.request.Request(
            self.service_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._request_json(request)

    def _request_json(
        self,
        request: urllib.request.Request,
    ) -> Dict[str, object]:
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.request_timeout_seconds,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
                message = payload.get("error")
            except Exception:
                message = str(error)
            raise ProxySchedulerError(str(message)) from error
        except OSError as error:
            raise ProxySchedulerError(
                f"无法连接统一代理调度服务 {self.service_url}：{error}"
            ) from error
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ProxySchedulerError("代理调度服务返回了无效 JSON")
        return payload


def main() -> int:
    try:
        load_dotenv(ENV_FILE)
        host = os.environ.get("PROXY_SCHEDULER_HOST", "127.0.0.1").strip()
        port = int(os.environ.get("PROXY_SCHEDULER_PORT", "8765"))
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("代理调度服务只允许绑定本机回环地址")
        with ProxyScheduler.from_env() as scheduler:
            service = ProxyLeaseService(scheduler)
            server = ProxySchedulerHTTPServer((host, port), service)
            print(
                f"[代理服务] 统一代理调度服务已启动：http://{host}:{port}；"
                f"当前代理 {scheduler.pool_size} 个",
                flush=True,
            )
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except Exception as error:
        print(f"[代理服务] 代理调度服务启动失败：{error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
