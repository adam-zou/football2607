"""Titan007 三类赔率变化页的抓取、DOM 提取与字段解析。"""

import asyncio
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from playwright.async_api import Browser, Page, async_playwright

from ..models import (
    HandicapChange,
    Movement,
    OddsSnapshot,
    OneXTwoChange,
    OverUnderChange,
)
from ..proxy import ProxyManager


# 浏览器返回的原始字典类型。它们和下方 dataclass 分开，形成清晰边界：
# 网页结构变化只需要改提取/解析层，其他模块继续使用稳定模型。
RawCell = Dict[str, Any]
RawRow = Dict[str, Any]
MarketChange = Union[HandicapChange, OneXTwoChange, OverUnderChange]


class Titan007OddsProvider:
    """抓取一场比赛在指定机构下的亚让、胜平负和进球数变化。"""

    BASE_URL = "https://vip.titan007.com/changeDetail/{endpoint}"
    COMPANIES = {
        3: "Crow*",
        4: "立*",
        8: "36*",
        24: "12*",
        31: "利*",
        47: "平*",
    }
    MARKETS = {
        "handicap": ("handicap.aspx", "#odds2 table"),
        "one_x_two": ("1x2.aspx", "#odds table"),
        "over_under": ("overunder.aspx", "#odds2 table"),
    }

    # 中文盘口转为可计算数值；“受让”符号在 parse_handicap_value 中处理。
    _HANDICAP_VALUES = {
        "平手": 0.0,
        "平手/半球": 0.25,
        "平/半": 0.25,
        "半球": 0.5,
        "半球/一球": 0.75,
        "半/一": 0.75,
        "一球": 1.0,
        "一球/球半": 1.25,
        "一/球半": 1.25,
        "球半": 1.5,
        "球半/两球": 1.75,
        "球半/两": 1.75,
        "两球": 2.0,
        "两球/两球半": 2.25,
        "两/两球半": 2.25,
        "两球半": 2.5,
        "两球半/三球": 2.75,
        "两球半/三": 2.75,
        "三球": 3.0,
        "三球/三球半": 3.25,
        "三/三球半": 3.25,
        "三球半": 3.5,
        "三球半/四球": 3.75,
        "三球半/四": 3.75,
        "四球": 4.0,
        "四球/四球半": 4.25,
        "四/四球半": 4.25,
        "四球半": 4.5,
        "四球半/五球": 4.75,
        "四球半/五": 4.75,
        "五球": 5.0,
    }

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        max_concurrency: int = 6,
        proxy_manager: ProxyManager,
    ) -> None:
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be greater than zero")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than zero")
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_concurrency = max_concurrency
        self.proxy_manager = proxy_manager

    async def fetch_match_odds(
        self,
        match_id: int,
        company_ids: Optional[Sequence[int]] = None,
    ) -> OddsSnapshot:
        """并发抓取“市场 × 机构”的全部页面，再按市场汇总为快照。"""

        match_id = self._validate_match_id(match_id)
        selected_companies = self._validate_company_ids(company_ids)

        proxy = await self.proxy_manager.get_proxy()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=self.headless,
                    proxy=proxy.playwright_options(),
                )
                try:
                    # 默认 6 家机构 × 3 个市场 = 18 页。Semaphore 防止 18 页
                    # 同时打开，降低本机资源和目标网站压力。
                    semaphore = asyncio.Semaphore(self.max_concurrency)

                    async def fetch_one(
                        market: str,
                        company_id: int,
                    ) -> Tuple[str, List[MarketChange]]:
                        async with semaphore:
                            rows = await self._fetch_page_rows(
                                browser,
                                match_id,
                                company_id,
                                market,
                            )
                            return market, self.parse_rows(
                                market,
                                rows,
                                match_id=match_id,
                                company_id=company_id,
                            )

                    # gather 保留传入协程的顺序，因此每个市场内机构顺序稳定。
                    results = await asyncio.gather(
                        *(
                            fetch_one(market, company_id)
                            for market in self.MARKETS
                            for company_id in selected_companies
                        )
                    )
                finally:
                    await browser.close()
        except asyncio.CancelledError:
            # 用户停止任务不代表当前代理有问题。
            raise
        except Exception:
            await self.proxy_manager.report_error()
            raise
        else:
            await self.proxy_manager.report_success()

        # fetch_one 为了能统一并发返回联合类型；这里按 market 拆回三个强类型
        # 列表，供 OddsSnapshot 和 JSON 消费者使用。
        handicap_changes: List[HandicapChange] = []
        one_x_two_changes: List[OneXTwoChange] = []
        over_under_changes: List[OverUnderChange] = []
        for market, changes in results:
            if market == "handicap":
                handicap_changes.extend(changes)  # type: ignore[arg-type]
            elif market == "one_x_two":
                one_x_two_changes.extend(changes)  # type: ignore[arg-type]
            else:
                over_under_changes.extend(changes)  # type: ignore[arg-type]

        return OddsSnapshot(
            match_id=match_id,
            companies={
                company_id: self.COMPANIES[company_id]
                for company_id in selected_companies
            },
            handicap_changes=handicap_changes,
            one_x_two_changes=one_x_two_changes,
            over_under_changes=over_under_changes,
        )

    async def _fetch_page_rows(
        self,
        browser: Browser,
        match_id: int,
        company_id: int,
        market: str,
    ) -> List[RawRow]:
        """打开一个机构的一个市场页面，并返回表格原始行。"""

        page = await browser.new_page(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        try:
            selector = self.MARKETS[market][1]
            await page.goto(
                self.build_url(match_id, company_id, market),
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            table = page.locator(selector)
            if await table.count() == 0:
                # 有些机构没有为本场提供某个市场；页面仍有导航但没有赔率表。
                # 这是合法的“空结果”，不能让整场比赛抓取失败。
                return []
            return await self._extract_rows(page, selector)
        finally:
            await page.close()

    @staticmethod
    async def _extract_rows(page: Page, selector: str) -> List[RawRow]:
        """在浏览器中抽取文本、合并列数和颜色三个解析所需信息。"""

        return await page.locator(selector).evaluate(
            """table => Array.from(table.rows).slice(1).map(row => ({
                cells: Array.from(row.cells).map(cell => {
                    const colored = cell.querySelector('font[color]');
                    return {
                        text: (cell.innerText || '').replace(/\\s+/g, ' ').trim(),
                        colSpan: cell.colSpan || 1,
                        color: colored ? (colored.getAttribute('color') || '') : ''
                    };
                })
            }))"""
        )

    @classmethod
    def build_url(cls, match_id: int, company_id: int, market: str) -> str:
        """校验输入并构造一个赔率变化页 URL。"""

        match_id = cls._validate_match_id(match_id)
        if company_id not in cls.COMPANIES:
            raise ValueError(f"unsupported company_id: {company_id}")
        try:
            endpoint = cls.MARKETS[market][0]
        except KeyError as error:
            raise ValueError(f"unsupported market: {market}") from error
        return (
            cls.BASE_URL.format(endpoint=endpoint)
            + f"?id={match_id}&companyid={company_id}&l=0"
        )

    @classmethod
    def parse_rows(
        cls,
        market: str,
        rows: Sequence[RawRow],
        *,
        match_id: int,
        company_id: int,
    ) -> List[MarketChange]:
        """按 DOM 顺序解析整张表，并为每行生成稳定的 seq。"""

        if market not in cls.MARKETS:
            raise ValueError(f"unsupported market: {market}")
        cls._validate_match_id(match_id)
        if company_id not in cls.COMPANIES:
            raise ValueError(f"unsupported company_id: {company_id}")

        total_rows = len(rows)
        changes: List[MarketChange] = []
        for dom_index, row in enumerate(rows):
            cells = row.get("cells")
            if not isinstance(cells, list):
                raise ValueError(f"row {dom_index + 1} has no cells")
            # 网页把最新记录放在顶部，但业务约定最早记录 seq=1，所以反向编号。
            seq = total_rows - dom_index
            changes.append(
                cls._parse_row(
                    market,
                    cells,
                    match_id=match_id,
                    company_id=company_id,
                    seq=seq,
                )
            )
        return changes

    @classmethod
    def _parse_row(
        cls,
        market: str,
        cells: Sequence[RawCell],
        *,
        match_id: int,
        company_id: int,
        seq: int,
    ) -> MarketChange:
        """解析一行；先提取公共字段，再根据市场创建对应模型。"""

        suspended = cls._is_suspended(cells)
        # 正常行有 7 列；封盘时中间三个市场值合并成一个“封”单元格，
        # 所以 DOM 中只剩 5 列，后面的时间/状态索引也随之提前。
        expected_cells = 5 if suspended else 7
        if len(cells) != expected_cells:
            raise ValueError(
                f"unexpected {market} row shape: {len(cells)} cells"
            )

        match_minute = cls._parse_match_minute(cls._text(cells[0]))
        home_score, away_score = cls._parse_score(cls._text(cells[1]))
        change_index = 3 if suspended else 5
        status_index = 4 if suspended else 6
        common: Dict[str, Any] = {
            "match_id": match_id,
            "company_id": company_id,
            "seq": seq,
            "match_minute": match_minute,
            "home_score": home_score,
            "away_score": away_score,
            "change_time": cls._text(cells[change_index]),
            "source_status": cls._text(cells[status_index]),
            "is_suspended": suspended,
        }

        if suspended:
            # 封盘记录仍保留比赛时间、比分和状态，只把报价字段置空。
            return cls._suspended_change(market, common)
        if market == "handicap":
            handicap_raw = cls._text(cells[3])
            return HandicapChange(
                **common,
                home_odds=cls._parse_float(cls._text(cells[2])),
                home_odds_movement=cls._movement(cells[2]),
                handicap_raw=handicap_raw,
                handicap_value=cls.parse_handicap_value(handicap_raw),
                handicap_movement=cls._movement(cells[3]),
                away_odds=cls._parse_float(cls._text(cells[4])),
                away_odds_movement=cls._movement(cells[4]),
            )
        if market == "one_x_two":
            return OneXTwoChange(
                **common,
                home_win_odds=cls._parse_float(cls._text(cells[2])),
                home_win_odds_movement=cls._movement(cells[2]),
                draw_odds=cls._parse_float(cls._text(cells[3])),
                draw_odds_movement=cls._movement(cells[3]),
                away_win_odds=cls._parse_float(cls._text(cells[4])),
                away_win_odds_movement=cls._movement(cells[4]),
            )

        total_line_raw = cls._text(cells[3])
        return OverUnderChange(
            **common,
            over_odds=cls._parse_float(cls._text(cells[2])),
            over_odds_movement=cls._movement(cells[2]),
            total_line_raw=total_line_raw,
            total_line_value=cls.parse_total_line_value(total_line_raw),
            total_line_movement=cls._movement(cells[3]),
            under_odds=cls._parse_float(cls._text(cells[4])),
            under_odds_movement=cls._movement(cells[4]),
        )

    @staticmethod
    def _suspended_change(
        market: str,
        common: Dict[str, Any],
    ) -> MarketChange:
        """根据市场创建所有报价字段均为 None 的封盘记录。"""

        if market == "handicap":
            return HandicapChange(
                **common,
                home_odds=None,
                home_odds_movement=None,
                handicap_raw=None,
                handicap_value=None,
                handicap_movement=None,
                away_odds=None,
                away_odds_movement=None,
            )
        if market == "one_x_two":
            return OneXTwoChange(
                **common,
                home_win_odds=None,
                home_win_odds_movement=None,
                draw_odds=None,
                draw_odds_movement=None,
                away_win_odds=None,
                away_win_odds_movement=None,
            )
        return OverUnderChange(
            **common,
            over_odds=None,
            over_odds_movement=None,
            total_line_raw=None,
            total_line_value=None,
            total_line_movement=None,
            under_odds=None,
            under_odds_movement=None,
        )

    @classmethod
    def parse_handicap_value(cls, value: str) -> Optional[float]:
        """把“半球”“受平/半”等中文亚让盘口转换为带符号数值。"""

        normalized = re.sub(r"\s+", "", value or "")
        if not normalized:
            return None
        negative = False
        # 站在主队角度：主队受让使用负数，主队让球使用正数。
        if normalized.startswith("受让"):
            negative = True
            normalized = normalized[2:]
        elif normalized.startswith("受"):
            negative = True
            normalized = normalized[1:]

        try:
            parsed = float(normalized)
        except ValueError:
            parsed = cls._HANDICAP_VALUES.get(normalized)  # type: ignore[assignment]
        if parsed is None:
            return None
        return -parsed if negative and parsed != 0 else parsed

    @staticmethod
    def parse_total_line_value(value: str) -> Optional[float]:
        """把进球数盘口转为数值；如 ``2/2.5`` 取中点 2.25。"""

        normalized = re.sub(r"\s+", "", value or "")
        if not normalized:
            return None
        try:
            parts = [float(part) for part in normalized.split("/")]
        except ValueError:
            return None
        if len(parts) == 1:
            return parts[0]
        if len(parts) == 2:
            return sum(parts) / 2
        return None

    @staticmethod
    def _movement(cell: RawCell) -> Movement:
        """Titan007 用红/绿字表示升/降，无颜色表示不变。"""

        color = str(cell.get("color") or "").strip().lower()
        if color == "red":
            return Movement.UP
        if color == "green":
            return Movement.DOWN
        return Movement.UNCHANGED

    @staticmethod
    def _is_suspended(cells: Sequence[RawCell]) -> bool:
        """通过“5 列 + 中间单元格跨 3 列显示封”识别封盘行。"""

        return (
            len(cells) == 5
            and Titan007OddsProvider._text(cells[2]) == "封"
            and int(cells[2].get("colSpan") or 1) == 3
        )

    @staticmethod
    def _parse_match_minute(value: str) -> Optional[int]:
        if not value:
            return None
        match = re.match(r"\d+", value)
        return int(match.group(0)) if match else None

    @staticmethod
    def _parse_score(value: str) -> Tuple[Optional[int], Optional[int]]:
        if not value:
            return None, None
        match = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)", value)
        if match is None:
            raise ValueError(f"invalid score: {value}")
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _parse_float(value: str) -> Optional[float]:
        if not value:
            return None
        try:
            return float(value)
        except ValueError as error:
            raise ValueError(f"invalid numeric value: {value}") from error

    @staticmethod
    def _text(cell: RawCell) -> str:
        return re.sub(r"\s+", " ", str(cell.get("text") or "")).strip()

    @staticmethod
    def _validate_match_id(match_id: int) -> int:
        try:
            parsed = int(match_id)
        except (TypeError, ValueError) as error:
            raise ValueError("match_id must be a positive integer") from error
        if parsed <= 0:
            raise ValueError("match_id must be a positive integer")
        return parsed

    @classmethod
    def _validate_company_ids(
        cls,
        company_ids: Optional[Sequence[int]],
    ) -> List[int]:
        selected = list(cls.COMPANIES) if company_ids is None else list(company_ids)
        if not selected:
            raise ValueError("at least one company_id is required")
        unsupported = [company_id for company_id in selected if company_id not in cls.COMPANIES]
        if unsupported:
            raise ValueError(f"unsupported company_id: {unsupported[0]}")
        return list(dict.fromkeys(selected))
