"""Titan007 比赛详情页的批量抓取与解析。"""

import asyncio
import logging
import re
from typing import AsyncIterator, Any, Dict, List, Optional, Sequence, Tuple

from playwright.async_api import Browser, async_playwright

from ..models import MatchBasicInfo
from ..observability import RuntimeObservability
from ..proxy import ProxyManager


logger = logging.getLogger(__name__)


class Titan007MatchDetailProvider:
    """从详情页收集列表页不够可靠的联赛和主客队等基本信息。"""

    SOURCE = "titan007"
    DEFAULT_URL_TEMPLATE = "https://live.titan007.com/detail/{match_id}sb.htm"
    HEADER_SELECTOR = "#header .analyhead"

    def __init__(
        self,
        url_template: str = DEFAULT_URL_TEMPLATE,
        *,
        headless: bool = True,
        timeout_ms: int = 10_000,
        max_concurrency: int = 2,
        proxy_manager: ProxyManager,
        observability: Optional[RuntimeObservability] = None,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than zero")
        self.url_template = url_template
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_concurrency = max_concurrency
        self.proxy_manager = proxy_manager
        self.observability = observability

    async def fetch_match_detail_batches(
        self,
        match_ids: Sequence[int],
        *,
        batch_size: int = 10,
    ) -> AsyncIterator[List[MatchBasicInfo]]:
        """按批抓取详情；每完成一批就产出一次，供调用方立即落库。"""

        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        if not match_ids:
            return

        proxy = await self.proxy_manager.get_proxy()
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    headless=self.headless,
                    proxy=proxy.playwright_options(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.proxy_manager.report_error()
                raise
            try:
                # Semaphore 只限制同时打开的页面数，不改变最终批次顺序。
                semaphore = asyncio.Semaphore(self.max_concurrency)

                async def fetch_one(match_id: int) -> Optional[MatchBasicInfo]:
                    async with semaphore:
                        try:
                            detail = await self._fetch_one(browser, match_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            if self.observability is not None:
                                self.observability.increment(
                                    "page_requests_total",
                                    provider="titan007_detail",
                                    result="failure",
                                )
                            # 单个比赛失败不应丢掉整批成功结果，因此记录日志后
                            # 返回 None，由下面的列表推导过滤。
                            await self.proxy_manager.report_error()
                            logger.exception(
                                "抓取比赛 %d 的 Titan007 详情失败",
                                match_id,
                            )
                            return None
                        else:
                            if self.observability is not None:
                                self.observability.increment(
                                    "page_requests_total",
                                    provider="titan007_detail",
                                    result="success",
                                )
                            await self.proxy_manager.report_success()
                            return detail

                for start in range(0, len(match_ids), batch_size):
                    # gather 并发等待本批；yield 让数据库可以先保存本批，再继续。
                    batch = match_ids[start : start + batch_size]
                    results = await asyncio.gather(
                        *(fetch_one(match_id) for match_id in batch)
                    )
                    yield [
                        detail for detail in results if detail is not None
                    ]
            finally:
                await browser.close()

    async def _fetch_one(
        self,
        browser: Browser,
        match_id: int,
    ) -> Optional[MatchBasicInfo]:
        """为单场比赛创建独立页面，提取 DOM 后确保关闭页面。"""

        page = await browser.new_page(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        try:
            await page.goto(
                self.url_template.format(match_id=match_id),
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            await page.wait_for_selector(
                self.HEADER_SELECTOR,
                state="attached",
                timeout=self.timeout_ms,
            )
            # 浏览器只负责选择 DOM；字段校验和转换统一留在 parse_detail 中。
            raw: Dict[str, Any] = await page.evaluate(
                """matchId => ({
                    matchId,
                    league: document.querySelector('#header .LName')?.innerText,
                    homeTeam: document.querySelector('#header .home a')?.innerText,
                    awayTeam: document.querySelector('#header .guest a')?.innerText,
                    scheduledTime: document.querySelector('#header .time')?.innerText,
                    scores: Array.from(
                        document.querySelectorAll('#headVs .score')
                    ).map(element => element.innerText),
                    statusText: document.querySelector('#mState')?.innerText
                })""",
                match_id,
            )
            return self.parse_detail(raw)
        finally:
            await page.close()

    @classmethod
    def parse_detail(cls, row: Dict[str, Any]) -> Optional[MatchBasicInfo]:
        """把详情 DOM 数据转为模型；关键字段缺失时放弃这一行。"""

        try:
            match_id = int(row["matchId"])
        except (KeyError, TypeError, ValueError):
            return None

        league = cls._clean_text(row.get("league"))
        home_team = cls._clean_text(row.get("homeTeam"))
        away_team = cls._clean_text(row.get("awayTeam"))
        scheduled_time = cls._clean_text(row.get("scheduledTime"))
        status_text = cls._clean_text(row.get("statusText")) or "未开始"
        scores = row.get("scores")

        if not league or not home_team or not away_team or not scheduled_time:
            return None

        home_score, away_score = cls._parse_scores(scores)
        return MatchBasicInfo(
            source=cls.SOURCE,
            match_id=match_id,
            league=league,
            home_team=home_team,
            away_team=away_team,
            scheduled_time=scheduled_time,
            home_score=home_score,
            away_score=away_score,
            status_text=status_text,
        )

    @staticmethod
    def _clean_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @classmethod
    def _parse_scores(
        cls,
        values: Any,
    ) -> Tuple[Optional[int], Optional[int]]:
        if not isinstance(values, list) or len(values) != 2:
            return None, None
        cleaned = [cls._clean_text(value) for value in values]
        if not all(re.fullmatch(r"\d+", value) for value in cleaned):
            return None, None
        return int(cleaned[0]), int(cleaned[1])
