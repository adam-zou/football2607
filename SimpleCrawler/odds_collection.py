"""Shared Titan007 odds-page collection and persistence module."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from lxml import html as lxml_html
from playwright.async_api import Browser as AsyncBrowser
from playwright.async_api import Page as AsyncPage
from playwright.sync_api import Browser, Page
from psycopg2.extensions import connection as Connection
from psycopg2.extras import execute_values

from simple_crawler.models import (
    HandicapChange,
    Movement,
    OddsChange,
    OneXTwoChange,
    OverUnderChange,
)
from simple_crawler.odds_parser import Titan007OddsParser

try:
    from .concurrent_pages import async_proxy_lease
    from .odds_market_state import (
        ensure_market_state_schema,
        record_market_result,
    )
except ImportError:
    from concurrent_pages import async_proxy_lease
    from odds_market_state import ensure_market_state_schema, record_market_result


BLOCKED_RESOURCE_TYPES = {
    "script",
    "stylesheet",
    "image",
    "media",
    "font",
}
MIN_CURRENT_PROXY_REMAINING_SECONDS = 1.0
ERROR_MARKERS = (
    "access denied",
    "forbidden",
    "request blocked",
    "waf",
    "访问被拒绝",
    "禁止访问",
    "安全验证",
    "验证码",
)
MARKETS = {
    "handicap": ("handicap.aspx", "#odds2 table", "亚让"),
    "one_x_two": ("1x2.aspx", "#odds table", "胜平负"),
    "over_under": ("overunder.aspx", "#odds2 table", "进球数"),
}

MarketChange = Union[HandicapChange, OneXTwoChange, OverUnderChange]

CREATE_ODDS_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS titan007_handicap_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    seq INTEGER NOT NULL CHECK (seq > 0),
    match_minute SMALLINT,
    home_score SMALLINT,
    away_score SMALLINT,
    change_time TEXT NOT NULL,
    source_status TEXT NOT NULL,
    is_suspended BOOLEAN NOT NULL,
    home_odds NUMERIC(8, 3),
    home_odds_movement TEXT
        CHECK (home_odds_movement IN ('上升', '下降', '不变')),
    handicap_raw TEXT,
    handicap_value NUMERIC(6, 2),
    handicap_movement TEXT
        CHECK (handicap_movement IN ('上升', '下降', '不变')),
    away_odds NUMERIC(8, 3),
    away_odds_movement TEXT
        CHECK (away_odds_movement IN ('上升', '下降', '不变')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id, seq),
    CHECK (
        NOT is_suspended OR (
            home_odds IS NULL
            AND home_odds_movement IS NULL
            AND handicap_raw IS NULL
            AND handicap_value IS NULL
            AND handicap_movement IS NULL
            AND away_odds IS NULL
            AND away_odds_movement IS NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS titan007_1x2_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    seq INTEGER NOT NULL CHECK (seq > 0),
    match_minute SMALLINT,
    home_score SMALLINT,
    away_score SMALLINT,
    change_time TEXT NOT NULL,
    source_status TEXT NOT NULL,
    is_suspended BOOLEAN NOT NULL,
    home_win_odds NUMERIC(8, 3),
    home_win_odds_movement TEXT
        CHECK (home_win_odds_movement IN ('上升', '下降', '不变')),
    draw_odds NUMERIC(8, 3),
    draw_odds_movement TEXT
        CHECK (draw_odds_movement IN ('上升', '下降', '不变')),
    away_win_odds NUMERIC(8, 3),
    away_win_odds_movement TEXT
        CHECK (away_win_odds_movement IN ('上升', '下降', '不变')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id, seq),
    CHECK (
        NOT is_suspended OR (
            home_win_odds IS NULL
            AND home_win_odds_movement IS NULL
            AND draw_odds IS NULL
            AND draw_odds_movement IS NULL
            AND away_win_odds IS NULL
            AND away_win_odds_movement IS NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS titan007_over_under_changes (
    match_id BIGINT NOT NULL,
    company_id INTEGER NOT NULL CHECK (company_id IN (3, 4, 8, 24, 31, 47)),
    seq INTEGER NOT NULL CHECK (seq > 0),
    match_minute SMALLINT,
    home_score SMALLINT,
    away_score SMALLINT,
    change_time TEXT NOT NULL,
    source_status TEXT NOT NULL,
    is_suspended BOOLEAN NOT NULL,
    over_odds NUMERIC(8, 3),
    over_odds_movement TEXT
        CHECK (over_odds_movement IN ('上升', '下降', '不变')),
    total_line_raw TEXT,
    total_line_value NUMERIC(6, 2),
    total_line_movement TEXT
        CHECK (total_line_movement IN ('上升', '下降', '不变')),
    under_odds NUMERIC(8, 3),
    under_odds_movement TEXT
        CHECK (under_odds_movement IN ('上升', '下降', '不变')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (match_id, company_id, seq),
    CHECK (
        NOT is_suspended OR (
            over_odds IS NULL
            AND over_odds_movement IS NULL
            AND total_line_raw IS NULL
            AND total_line_value IS NULL
            AND total_line_movement IS NULL
            AND under_odds IS NULL
            AND under_odds_movement IS NULL
        )
    )
)
"""

COMMON_VALUE_COLUMNS = (
    "match_minute",
    "home_score",
    "away_score",
    "change_time",
    "source_status",
    "is_suspended",
)
MARKET_VALUE_COLUMNS = {
    "handicap": (
        "home_odds",
        "home_odds_movement",
        "handicap_raw",
        "handicap_value",
        "handicap_movement",
        "away_odds",
        "away_odds_movement",
    ),
    "one_x_two": (
        "home_win_odds",
        "home_win_odds_movement",
        "draw_odds",
        "draw_odds_movement",
        "away_win_odds",
        "away_win_odds_movement",
    ),
    "over_under": (
        "over_odds",
        "over_odds_movement",
        "total_line_raw",
        "total_line_value",
        "total_line_movement",
        "under_odds",
        "under_odds_movement",
    ),
}
MARKET_TABLES = {
    "handicap": "titan007_handicap_changes",
    "one_x_two": "titan007_1x2_changes",
    "over_under": "titan007_over_under_changes",
}


def _build_upsert(market: str) -> str:
    table = MARKET_TABLES[market]
    mutable_columns = COMMON_VALUE_COLUMNS + MARKET_VALUE_COLUMNS[market]
    insert_columns = ("match_id", "company_id", "seq") + mutable_columns
    assignments = ",\n    ".join(
        f"{column} = EXCLUDED.{column}" for column in mutable_columns
    )
    existing_values = ",\n    ".join(
        f"{table}.{column}" for column in mutable_columns
    )
    excluded_values = ",\n    ".join(
        f"EXCLUDED.{column}" for column in mutable_columns
    )
    return f"""
INSERT INTO {table} (
    {', '.join(insert_columns)}
)
VALUES %s
ON CONFLICT (match_id, company_id, seq) DO UPDATE SET
    {assignments},
    updated_at = NOW()
WHERE (
    {existing_values}
) IS DISTINCT FROM (
    {excluded_values}
)
"""


UPSERT_HANDICAP = _build_upsert("handicap")
UPSERT_ONE_X_TWO = _build_upsert("one_x_two")
UPSERT_OVER_UNDER = _build_upsert("over_under")
UPSERTS = {
    "handicap": UPSERT_HANDICAP,
    "one_x_two": UPSERT_ONE_X_TWO,
    "over_under": UPSERT_OVER_UNDER,
}


def ensure_odds_schema(cursor: Any) -> None:
    cursor.execute(CREATE_ODDS_TABLES_SQL)
    ensure_market_state_schema(cursor)


@dataclass(frozen=True)
class OddsPageJob:
    match_id: int
    company_id: int
    market: str

    def __post_init__(self) -> None:
        if self.market not in MARKETS:
            raise ValueError(f"unsupported market: {self.market}")


@dataclass(frozen=True)
class OddsCompanyJob:
    match_id: int
    company_id: int
    markets: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.markets:
            raise ValueError("company job must contain at least one market")
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("company job markets must be unique")
        unsupported = [market for market in self.markets if market not in MARKETS]
        if unsupported:
            raise ValueError(f"unsupported markets: {unsupported}")

    def page_jobs(self) -> List[OddsPageJob]:
        return [
            OddsPageJob(self.match_id, self.company_id, market)
            for market in self.markets
        ]


@dataclass(frozen=True)
class OddsCollectionConfig:
    base_url: str
    timeout_seconds: float


@dataclass(frozen=True)
class MarketCollectionOutcome:
    job: OddsPageJob
    changes: Optional[List[MarketChange]] = None
    error: Optional[Exception] = None


class PartialCompanyCollectionError(RuntimeError):
    """Retire a partially failed proxy while preserving market outcomes."""

    def __init__(self, outcomes: List[MarketCollectionOutcome]) -> None:
        super().__init__("one or more company market pages failed")
        self.outcomes = outcomes


def group_page_jobs_by_company(
    jobs: Sequence[OddsPageJob],
) -> List[OddsCompanyJob]:
    grouped: Dict[Tuple[int, int], List[str]] = {}
    for job in jobs:
        key = (job.match_id, job.company_id)
        markets = grouped.setdefault(key, [])
        if job.market not in markets:
            markets.append(job.market)
    return [
        OddsCompanyJob(match_id, company_id, tuple(markets))
        for (match_id, company_id), markets in grouped.items()
    ]


def build_url(config: OddsCollectionConfig, job: OddsPageJob) -> str:
    endpoint = MARKETS[job.market][0]
    return (
        config.base_url.format(endpoint=endpoint)
        + f"?id={job.match_id}&companyid={job.company_id}&l=0"
    )


def block_unneeded_resources(route: Any) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


async def block_unneeded_resources_async(route: Any) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


def extract_page_rows_from_html(
    html: str,
    selector: str,
) -> List[Dict[str, Any]]:
    """Parse the expected market table from the main document response."""

    target, separator, element = selector.partition(" ")
    if not separator or not target.startswith("#") or element != "table":
        raise ValueError(f"不支持的赔率选择器：{selector}")
    document = lxml_html.fromstring(html)
    target_id = target[1:]
    tables = document.xpath(
        "//*[@id=$target_id]//table",
        target_id=target_id,
    )
    market_shell = document.xpath("//*[@id='odds' or @id='odds2']")
    market_navigation = document.xpath(
        "//a[contains(@href, 'handicap.aspx') "
        "or contains(@href, '1x2.aspx') "
        "or contains(@href, 'overunder.aspx')]"
    )
    for hidden in document.xpath("//script|//style"):
        hidden.drop_tree()
    visible_text = " ".join(document.text_content().split())
    state = {
        "bodyText": visible_text,
        "hasExpectedTable": bool(tables),
        "hasMarketShell": bool(market_shell),
        "hasMarketNavigation": bool(market_navigation),
    }
    if not validate_page_state(state):
        return []
    rows = []
    for row in tables[0].xpath(".//tr")[1:]:
        cells = []
        for cell in row.xpath("./th|./td"):
            colored = cell.xpath(".//font[@color]")
            try:
                colspan = int(cell.get("colspan", "1") or "1")
            except (TypeError, ValueError):
                colspan = 1
            cells.append(
                {
                    "text": " ".join(cell.text_content().split()),
                    "colSpan": max(colspan, 1),
                    "color": colored[0].get("color", "") if colored else "",
                }
            )
        rows.append({"cells": cells})
    return rows


def validate_page_state(state: Dict[str, Any]) -> bool:
    """Return true for a data table, false for a valid empty market page."""

    text = f"{state.get('title', '')} {state.get('bodyText', '')}".lower()
    if any(marker in text for marker in ERROR_MARKERS):
        raise RuntimeError("赔率页面是拦截页或错误页")
    if state.get("hasExpectedTable"):
        return True
    if state.get("hasMarketShell") or state.get("hasMarketNavigation"):
        return False
    raise RuntimeError("赔率页面缺少预期的市场结构")


def fetch_page_rows(
    page: Page,
    url: str,
    selector: str,
    timeout_seconds: float,
) -> List[Dict[str, Any]]:
    response = page.goto(
        url,
        wait_until="commit",
        timeout=int(timeout_seconds * 1000),
    )
    if response is None or response.status >= 400:
        status = "无响应" if response is None else response.status
        raise RuntimeError(f"赔率页面返回 HTTP {status}")
    return extract_page_rows_from_html(response.text(), selector)


async def fetch_page_rows_async(
    page: AsyncPage,
    url: str,
    selector: str,
    timeout_seconds: float,
) -> List[Dict[str, Any]]:
    response = await page.goto(
        url,
        wait_until="commit",
        timeout=int(timeout_seconds * 1000),
    )
    if response is None or response.status >= 400:
        status = "无响应" if response is None else response.status
        raise RuntimeError(f"赔率页面返回 HTTP {status}")
    return extract_page_rows_from_html(await response.text(), selector)


def collect_market_page(
    browser: Browser,
    proxy_client: Any,
    config: OddsCollectionConfig,
    job: OddsPageJob,
) -> List[MarketChange]:
    """Collect one market page with one proxy lease and browser context."""

    minimum_lifetime = min(
        config.timeout_seconds + 2,
        proxy_client.ttl_seconds - 1,
    )
    with proxy_client.lease(
        min_remaining_seconds=minimum_lifetime,
    ) as proxy:
        context = browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            proxy=proxy.playwright_options(),
        )
        try:
            page = context.new_page()
            page.route("**/*", block_unneeded_resources)
            rows = fetch_page_rows(
                page,
                build_url(config, job),
                MARKETS[job.market][1],
                config.timeout_seconds,
            )
            return Titan007OddsParser.parse_rows(
                job.market,
                rows,
                match_id=job.match_id,
                company_id=job.company_id,
            )
        finally:
            context.close()


async def collect_market_page_async(
    browser: AsyncBrowser,
    proxy_client: Any,
    config: OddsCollectionConfig,
    job: OddsPageJob,
) -> List[MarketChange]:
    """Async adapter with the same page semantics as ``collect_market_page``."""

    minimum_lifetime = min(
        config.timeout_seconds + 2,
        proxy_client.ttl_seconds - 1,
    )
    async with async_proxy_lease(
        proxy_client,
        min_remaining_seconds=minimum_lifetime,
    ) as proxy:
        context = await browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            proxy=proxy.playwright_options(),
        )
        try:
            page = await context.new_page()
            await page.route("**/*", block_unneeded_resources_async)
            rows = await fetch_page_rows_async(
                page,
                build_url(config, job),
                MARKETS[job.market][1],
                config.timeout_seconds,
            )
            return Titan007OddsParser.parse_rows(
                job.market,
                rows,
                match_id=job.match_id,
                company_id=job.company_id,
            )
        finally:
            await context.close()


async def collect_company_markets_async(
    browser: AsyncBrowser,
    proxy_client: Any,
    config: OddsCollectionConfig,
    company_job: OddsCompanyJob,
) -> List[MarketCollectionOutcome]:
    """Collect one company's requested markets in one proxy-bound context."""

    page_jobs = company_job.page_jobs()
    try:
        async with async_proxy_lease(
            proxy_client,
            min_remaining_seconds=MIN_CURRENT_PROXY_REMAINING_SECONDS,
            page_assignments=len(page_jobs),
        ) as proxy:
            context = await browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                proxy=proxy.playwright_options(),
            )
            try:
                page = await context.new_page()
                await page.route("**/*", block_unneeded_resources_async)
                outcomes: List[MarketCollectionOutcome] = []
                for job in page_jobs:
                    try:
                        rows = await fetch_page_rows_async(
                            page,
                            build_url(config, job),
                            MARKETS[job.market][1],
                            config.timeout_seconds,
                        )
                        changes = Titan007OddsParser.parse_rows(
                            job.market,
                            rows,
                            match_id=job.match_id,
                            company_id=job.company_id,
                        )
                    except Exception as error:
                        outcomes.append(
                            MarketCollectionOutcome(job=job, error=error)
                        )
                    else:
                        outcomes.append(
                            MarketCollectionOutcome(
                                job=job,
                                changes=changes,
                            )
                        )
                if any(outcome.error is not None for outcome in outcomes):
                    raise PartialCompanyCollectionError(outcomes)
                return outcomes
            finally:
                await context.close()
    except PartialCompanyCollectionError as error:
        return error.outcomes


def _movement_value(movement: Optional[Movement]) -> Optional[str]:
    return movement.value if movement is not None else None


def _common_values(change: OddsChange) -> Tuple[Any, ...]:
    return (
        change.match_id,
        change.company_id,
        change.seq,
        change.match_minute,
        change.home_score,
        change.away_score,
        change.change_time,
        change.source_status,
        change.is_suspended,
    )


def change_values(change: MarketChange) -> Tuple[Any, ...]:
    if isinstance(change, HandicapChange):
        return _common_values(change) + (
            change.home_odds,
            _movement_value(change.home_odds_movement),
            change.handicap_raw,
            change.handicap_value,
            _movement_value(change.handicap_movement),
            change.away_odds,
            _movement_value(change.away_odds_movement),
        )
    if isinstance(change, OneXTwoChange):
        return _common_values(change) + (
            change.home_win_odds,
            _movement_value(change.home_win_odds_movement),
            change.draw_odds,
            _movement_value(change.draw_odds_movement),
            change.away_win_odds,
            _movement_value(change.away_win_odds_movement),
        )
    return _common_values(change) + (
        change.over_odds,
        _movement_value(change.over_odds_movement),
        change.total_line_raw,
        change.total_line_value,
        _movement_value(change.total_line_movement),
        change.under_odds,
        _movement_value(change.under_odds_movement),
    )


def persist_market_page(
    connection: Connection,
    job: OddsPageJob,
    changes: Sequence[MarketChange],
    *,
    final: bool = False,
) -> None:
    """Atomically persist parsed changes and their successful page state."""

    values = [change_values(change) for change in changes]
    with connection.cursor() as cursor:
        if values:
            execute_values(
                cursor,
                UPSERTS[job.market],
                values,
                page_size=500,
            )
        record_market_result(
            cursor,
            match_id=job.match_id,
            company_id=job.company_id,
            market=job.market,
            rows=values,
            final=final,
        )
    connection.commit()


def persist_market_failure(
    connection: Connection,
    job: OddsPageJob,
    error: str,
    *,
    final: bool = False,
) -> None:
    """Persist one failed page attempt without replacing prior success data."""

    with connection.cursor() as cursor:
        record_market_result(
            cursor,
            match_id=job.match_id,
            company_id=job.company_id,
            market=job.market,
            error=error,
            final=final,
        )
    connection.commit()
