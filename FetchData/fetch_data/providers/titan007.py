"""从 Titan007 比赛列表页发现比赛 ID。"""

import asyncio
import re
from typing import List, Optional

from playwright.async_api import Page, async_playwright

from ..observability import RuntimeObservability
from ..proxy import ProxyManager


class Titan007Provider:
    """从 Titan007 已渲染的实时比分表发现比赛 ID。"""

    DEFAULT_URL = "https://live.titan007.com/oldIndexall.aspx"
    ROW_SELECTOR = 'tr[id^="tr1_"]'

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        headless: bool = True,
        timeout_ms: int = 10_000,
        settle_ms: int = 1_000,
        proxy_manager: ProxyManager,
        observability: Optional[RuntimeObservability] = None,
    ) -> None:
        self.url = url
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.proxy_manager = proxy_manager
        self.observability = observability

    async def fetch_match_ids(self) -> List[int]:
        """从列表页发现比赛 ID，并向代理管理器报告成败。"""

        proxy = await self.proxy_manager.get_proxy()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=self.headless,
                    proxy=proxy.playwright_options(),
                )
                try:
                    page = await browser.new_page(
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                    )
                    match_ids = await self._fetch_match_ids_from_page(page)
                finally:
                    await browser.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.observability is not None:
                self.observability.increment(
                    "page_requests_total",
                    provider="titan007_list",
                    result="failure",
                )
            await self.proxy_manager.report_error()
            raise
        else:
            if self.observability is not None:
                self.observability.increment(
                    "page_requests_total",
                    provider="titan007_list",
                    result="success",
                )
            await self.proxy_manager.report_success()
            return match_ids

    async def _fetch_match_ids_from_page(self, page: Page) -> List[int]:
        """只读取 ``tr1_<ID>``，基础信息完整性不影响比赛发现。"""

        await page.goto(
            self.url,
            wait_until="domcontentloaded",
            timeout=self.timeout_ms,
        )
        await page.wait_for_selector(
            self.ROW_SELECTOR,
            state="attached",
            timeout=self.timeout_ms,
        )
        if self.settle_ms:
            await page.wait_for_timeout(self.settle_ms)
        row_ids = await page.locator(self.ROW_SELECTOR).evaluate_all(
            "rows => rows.map(row => row.id)"
        )
        discovered: List[int] = []
        seen = set()
        for row_id in row_ids:
            matched = re.fullmatch(r"tr1_(\d+)", str(row_id))
            if matched is None:
                continue
            match_id = int(matched.group(1))
            if match_id > 0 and match_id not in seen:
                seen.add(match_id)
                discovered.append(match_id)
        return discovered
