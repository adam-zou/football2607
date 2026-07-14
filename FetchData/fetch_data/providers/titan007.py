"""Titan007 比赛列表页的抓取与解析。"""

import asyncio
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from playwright.async_api import Page, async_playwright

from ..models import Match, MatchStatus
from ..observability import RuntimeObservability
from ..proxy import ProxyManager


class Titan007Provider:
    """从 Titan007 已渲染的实时比分表收集比赛。

    页面会通过 JavaScript/WebSocket 创建并更新比赛行。直接读取最终 DOM，可以
    不依赖站点内部的压缩通信协议。比赛行目前遵循稳定的 ``tr1_<比赛 ID>`` 格式。
    """

    SOURCE = "titan007"
    DEFAULT_URL = "https://live.titan007.com/oldIndexall.aspx"
    ROW_SELECTOR = 'tr[id^="tr1_"]'

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
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

    async def fetch_matches(self) -> List[Match]:
        """用一个临时浏览器抓取快照，并向代理管理器报告成败。"""

        proxy = await self.proxy_manager.get_proxy()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=self.headless,
                    proxy=proxy.playwright_options(),
                )
                try:
                    # 固定语言和时区，让页面文字及比赛时间在不同机器上一致。
                    page = await browser.new_page(
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                    )
                    matches = await self._fetch_from_page(page)
                finally:
                    await browser.close()
        except asyncio.CancelledError:
            # 程序关闭不算代理失败，继续把取消信号向上传递。
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
            return matches

    async def _fetch_from_page(self, page: Page) -> List[Match]:
        """等待比赛行出现，提取最小 DOM 数据后交给纯 Python 解析器。"""

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

        # 给第一次实时推送留一点时间。这里只抓“此刻快照”，不是在浏览器中
        # 长期监听比分，所以等待时间特意保持很短。
        if self.settle_ms:
            await page.wait_for_timeout(self.settle_ms)

        # 列表的时间单元格只显示 HH:MM。页面用于渲染行的 A 数组还保留了
        # 年、月日和时间，必须在这里合并，避免列表刷新把详情页提供的完整
        # 开赛时间覆盖成只有时分的值。
        rows: List[Dict[str, Any]] = await page.locator(
            self.ROW_SELECTOR
        ).evaluate_all(
            """rows => rows.map(row => {
                const index = Number.parseInt(row.getAttribute('index'), 10);
                const matchData = Number.isInteger(index) ? window.A?.[index] : null;
                const year = String(matchData?.[43] ?? '').trim();
                const monthDay = String(matchData?.[36] ?? '').trim();
                const clock = String(matchData?.[11] ?? '').trim();
                const dateMatch = /^(\\d{4})-(\\d{1,2})-(\\d{1,2})$/.exec(
                    `${year}-${monthDay}`
                );
                const timeMatch = /^(\\d{1,2}):(\\d{2})$/.exec(clock);
                const scheduledTime = dateMatch && timeMatch
                    ? `${dateMatch[1]}-${dateMatch[2].padStart(2, '0')}-${
                        dateMatch[3].padStart(2, '0')
                    } ${timeMatch[1].padStart(2, '0')}:${timeMatch[2]}`
                    : '';

                return {
                    rowId: row.id,
                    scheduledTime,
                    cells: Array.from(row.cells).map(cell =>
                        (cell.innerText || '').replace(/\\s+/g, ' ').trim()
                    )
                };
            })"""
        )
        return self.parse_rows(rows)

    @classmethod
    def parse_rows(cls, rows: Iterable[Dict[str, Any]]) -> List[Match]:
        """忽略无效行和重复 ID，返回干净的比赛列表。"""

        matches: List[Match] = []
        seen_ids = set()

        for row in rows:
            match = cls.parse_row(row)
            if match is None or match.match_id in seen_ids:
                continue
            seen_ids.add(match.match_id)
            matches.append(match)

        return matches

    @classmethod
    def parse_row(cls, row: Dict[str, Any]) -> Optional[Match]:
        """按照固定列位置，把一个 DOM 行转换为 Match；不合法则返回 None。"""

        row_id = str(row.get("rowId", ""))
        match_id_match = re.fullmatch(r"tr1_(\d+)", row_id)
        cells = row.get("cells")
        if not match_id_match or not isinstance(cells, list) or len(cells) < 7:
            return None

        # Titan007 表格的列含义由页面结构决定，索引映射详见 FetchData/README。
        values = [cls._clean_text(value) for value in cells]
        league = values[1]
        scheduled_time = cls._clean_text(row.get("scheduledTime"))
        status_text = values[3]
        home_team = values[4]
        score_text = values[5]
        away_team = values[6]

        if (
            not league
            or not home_team
            or not away_team
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", scheduled_time)
        ):
            return None

        score, home_score, away_score = cls._parse_score(score_text)
        return Match(
            source=cls.SOURCE,
            match_id=match_id_match.group(1),
            league=league,
            home_team=home_team,
            away_team=away_team,
            score=score,
            home_score=home_score,
            away_score=away_score,
            status=cls._parse_status(status_text),
            status_text=status_text,
            scheduled_time=scheduled_time,
        )

    @staticmethod
    def _clean_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _parse_score(value: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
        match = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)", value)
        if match is None:
            return None, None, None
        home_score, away_score = int(match.group(1)), int(match.group(2))
        return f"{home_score}-{away_score}", home_score, away_score

    @staticmethod
    def _parse_status(value: str) -> MatchStatus:
        """把网页的中英文状态文本归一为稳定枚举，同时保留未知状态。"""

        normalized = value.strip().lower()
        if not normalized:
            return MatchStatus.SCHEDULED
        if normalized in {"完", "完场", "finished", "ft"}:
            return MatchStatus.FINISHED
        if normalized in {"中", "中场", "半", "half", "ht"}:
            return MatchStatus.HALF_TIME
        if normalized in {"推迟", "延期", "postponed"}:
            return MatchStatus.POSTPONED
        if normalized in {"取消", "cancelled", "canceled"}:
            return MatchStatus.CANCELLED
        if normalized in {"腰斩", "中断", "abandoned"}:
            return MatchStatus.ABANDONED
        if re.fullmatch(r"\d{1,3}(?:\+\d{1,2})?['′]?", normalized):
            return MatchStatus.LIVE
        return MatchStatus.UNKNOWN
