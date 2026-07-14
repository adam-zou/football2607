"""代理的获取、校验、缓存和失效管理。

抓取器不直接读取代理 API，也不自己统计失败；它们统一调用 ProxyManager，
从而让比赛列表、详情和赔率抓取共享同一套轮换规则。
"""

import asyncio
import base64
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional
from urllib.parse import urlparse

from .observability import RuntimeObservability


DEFAULT_PROXY_UPDATE_INTERVAL = 60.0
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3
DEFAULT_PROXY_API_TIMEOUT = 5.0
DEFAULT_PROXY_TEST_TIMEOUT = 5.0
DEFAULT_PROXY_TEST_URL = "https://live.nowscore.com"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}


class ProxyError(RuntimeError):
    """Raised when a usable proxy cannot be obtained."""


@dataclass(frozen=True)
class ProxySettings:
    """一个已经规范化的带认证代理；密码不会出现在 repr/日志中。"""

    server: str
    username: str
    password: str = field(repr=False)

    def playwright_options(self) -> Dict[str, str]:
        """转换成 Playwright ``launch(proxy=...)`` 需要的字典。"""

        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }


# 把网络函数声明成可注入的类型，测试可以传入假函数而不访问真实供应商。
ProxyFetcher = Callable[[str, Mapping[str, str], float], str]
ProxyValidator = Callable[
    [ProxySettings, str, Mapping[str, str], float],
    bool,
]
Clock = Callable[[], float]


class ProxyManager:
    """获取、验证并缓存一个带认证的 HTTP 代理，必要时使其失效。"""

    def __init__(
        self,
        *,
        api_url: str,
        username: str,
        password: str,
        update_interval: float = DEFAULT_PROXY_UPDATE_INTERVAL,
        max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
        test_url: str = DEFAULT_PROXY_TEST_URL,
        api_timeout: float = DEFAULT_PROXY_API_TIMEOUT,
        test_timeout: float = DEFAULT_PROXY_TEST_TIMEOUT,
        headers: Optional[Mapping[str, str]] = None,
        fetcher: Optional[ProxyFetcher] = None,
        validator: Optional[ProxyValidator] = None,
        clock: Clock = time.monotonic,
        observability: Optional[RuntimeObservability] = None,
    ) -> None:
        if not api_url.strip():
            raise ValueError("api_url is required")
        if not username.strip():
            raise ValueError("username is required")
        if not password:
            raise ValueError("password is required")
        if update_interval <= 0:
            raise ValueError("update_interval must be greater than zero")
        if max_consecutive_errors <= 0:
            raise ValueError("max_consecutive_errors must be greater than zero")
        if api_timeout <= 0 or test_timeout <= 0:
            raise ValueError("proxy timeouts must be greater than zero")
        if not test_url.strip():
            raise ValueError("test_url is required")

        self.api_url = api_url
        self.username = username
        self.password = password
        self.update_interval = update_interval
        self.max_consecutive_errors = max_consecutive_errors
        self.test_url = test_url
        self.api_timeout = api_timeout
        self.test_timeout = test_timeout
        self.headers = dict(headers or DEFAULT_HEADERS)
        self._fetcher = fetcher or self._fetch_proxy_text
        self._validator = validator or self._validate_proxy
        self._clock = clock
        self.observability = observability
        # 以下是会随运行变化的缓存状态，不来自环境变量。
        self._proxy: Optional[ProxySettings] = None
        self._updated_at = 0.0
        self._consecutive_errors = 0
        self._lock: Optional[asyncio.Lock] = None

    @classmethod
    def from_env(
        cls,
        observability: Optional[RuntimeObservability] = None,
    ) -> "ProxyManager":
        """读取环境变量，并尽早报告缺少或格式错误的配置。"""

        required = {
            "PROXY_API_URL": os.environ.get("PROXY_API_URL"),
            "PROXY_USERNAME": os.environ.get("PROXY_USERNAME"),
            "PROXY_PASSWORD": os.environ.get("PROXY_PASSWORD"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "missing required proxy configuration: " + ", ".join(missing)
            )

        return cls(
            api_url=required["PROXY_API_URL"] or "",
            username=required["PROXY_USERNAME"] or "",
            password=required["PROXY_PASSWORD"] or "",
            update_interval=cls._float_env(
                "PROXY_UPDATE_INTERVAL",
                DEFAULT_PROXY_UPDATE_INTERVAL,
            ),
            max_consecutive_errors=cls._int_env(
                "PROXY_MAX_CONSECUTIVE_ERRORS",
                DEFAULT_MAX_CONSECUTIVE_ERRORS,
            ),
            test_url=os.environ.get("PROXY_TEST_URL", DEFAULT_PROXY_TEST_URL),
            api_timeout=cls._float_env(
                "PROXY_API_TIMEOUT",
                DEFAULT_PROXY_API_TIMEOUT,
            ),
            test_timeout=cls._float_env(
                "PROXY_TEST_TIMEOUT",
                DEFAULT_PROXY_TEST_TIMEOUT,
            ),
            observability=observability,
        )

    async def get_proxy(self) -> ProxySettings:
        """返回仍在有效期的缓存代理，否则获取并验证一个新代理。"""

        async with self._get_lock():
            now = self._clock()
            # 缓存未过期时不访问供应商，减少 API 次数和代理抖动。
            if (
                self._proxy is not None
                and now - self._updated_at < self.update_interval
            ):
                if self.observability is not None:
                    self.observability.increment(
                        "proxy_requests_total", result="cache_hit"
                    )
                return self._proxy

            if self.observability is not None:
                self.observability.increment(
                    "proxy_requests_total", result="refresh"
                )
            try:
                # urllib 是同步 API，放进线程后不会卡住 Playwright 的事件循环。
                response = await asyncio.to_thread(
                    self._fetcher,
                    self.api_url,
                    self.headers,
                    self.api_timeout,
                )
                proxy = self.parse_proxy_response(
                    response,
                    username=self.username,
                    password=self.password,
                )
            except (ProxyError, ValueError):
                if self.observability is not None:
                    self.observability.record_health("proxy", False, "fetch failed")
                raise
            except Exception:
                if self.observability is not None:
                    self.observability.record_health("proxy", False, "fetch failed")
                raise ProxyError("failed to fetch proxy from supplier") from None

            try:
                is_valid = await asyncio.to_thread(
                    self._validator,
                    proxy,
                    self.test_url,
                    self.headers,
                    self.test_timeout,
                )
            except Exception:
                if self.observability is not None:
                    self.observability.increment(
                        "proxy_validation_total", result="failure"
                    )
                    self.observability.record_health(
                        "proxy", False, "validation failed"
                    )
                raise ProxyError("proxy validation failed") from None
            if not is_valid:
                if self.observability is not None:
                    self.observability.increment(
                        "proxy_validation_total", result="failure"
                    )
                    self.observability.record_health(
                        "proxy", False, "validation failed"
                    )
                raise ProxyError("proxy validation failed")

            # 只有“成功获取 + 成功验证”后才替换旧缓存。
            self._proxy = proxy
            self._updated_at = now
            self._consecutive_errors = 0
            if self.observability is not None:
                self.observability.increment(
                    "proxy_validation_total", result="success"
                )
                self.observability.record_health("proxy", True)
            return proxy

    async def report_success(self) -> None:
        """一次成功访问会清零连续失败次数。"""

        async with self._get_lock():
            self._consecutive_errors = 0

    async def report_error(self) -> None:
        """累计连续失败；达到阈值后让下一次请求强制获取新代理。"""

        async with self._get_lock():
            self._consecutive_errors += 1
            if self.observability is not None:
                self.observability.increment("proxy_access_errors_total")
            if self._consecutive_errors >= self.max_consecutive_errors:
                # 不在这里立刻访问供应商。真正需要代理的调用会通过
                # get_proxy 获取，保持职责和异常处理简单。
                self._proxy = None
                self._updated_at = 0.0
                if self.observability is not None:
                    self.observability.increment("proxy_invalidations_total")

    def _get_lock(self) -> asyncio.Lock:
        """惰性创建事件循环锁，保护缓存刷新和失败计数。"""

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def parse_proxy_response(
        response: str,
        *,
        username: str,
        password: str,
    ) -> ProxySettings:
        """严格解析供应商返回的单个 host:port，拒绝额外路径和凭据。"""

        lines = [line.strip() for line in response.splitlines() if line.strip()]
        if len(lines) != 1:
            raise ProxyError("proxy supplier returned an unexpected response")

        raw_server = lines[0]
        # urlparse 只有看到 scheme 才会把 host:port 识别为网络地址。
        candidate = raw_server if "://" in raw_server else f"http://{raw_server}"
        parsed = urlparse(candidate)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ProxyError("proxy supplier returned an invalid proxy address")

        host = parsed.hostname
        # IPv6 地址在 URL 中必须包在方括号里，IPv4/域名则保持原样。
        if ":" in host:
            host = f"[{host}]"
        return ProxySettings(
            server=f"{parsed.scheme}://{host}:{port}",
            username=username,
            password=password,
        )

    @staticmethod
    def _fetch_proxy_text(
        api_url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> str:
        """调用代理供应商 API，返回未经解析的响应正文。"""

        request = urllib.request.Request(api_url, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except Exception:
            raise ProxyError("failed to fetch proxy from supplier") from None

    @staticmethod
    def _validate_proxy(
        proxy: ProxySettings,
        test_url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> bool:
        """通过代理访问测试地址，仅把 2xx/3xx 响应视为可用。"""

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
        request = urllib.request.Request(test_url, headers=dict(headers))
        # HTTPS 代理认证发生在 CONNECT 建立隧道之前。urllib 的认证处理器
        # 无法在 CONNECT 返回 407 后重试，因此首次请求必须主动携带凭据。
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
        try:
            return default if raw is None else float(raw)
        except ValueError:
            raise ValueError(f"{name} must be a number") from None

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        try:
            return default if raw is None else int(raw)
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None
