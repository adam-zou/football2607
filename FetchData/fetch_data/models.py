"""项目中各层共享的数据模型。

可以把这里理解成项目的“统一数据格式”：Provider 负责把网页内容转换成这些
对象，存储层和命令行层只消费对象，不需要知道网页 DOM 长什么样。
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class Movement(str, Enum):
    """赔率或盘口相对上一条历史记录的变化方向。"""

    UP = "上升"
    DOWN = "下降"
    UNCHANGED = "不变"


@dataclass(frozen=True)
class MatchBasicInfo:
    """详情页提供、并写入 ``match_basic_info`` 表的比赛基本信息。"""

    source: str
    match_id: int
    league: str
    home_team: str
    away_team: str
    scheduled_time: str
    home_score: Optional[int]
    away_score: Optional[int]
    status_text: str


@dataclass(frozen=True)
class OddsChange:
    """三类赔率变动共有的字段，具体市场字段由子类补充。"""

    match_id: int
    company_id: int
    seq: int
    match_minute: Optional[int]
    home_score: Optional[int]
    away_score: Optional[int]
    change_time: str
    source_status: str
    is_suspended: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HandicapChange(OddsChange):
    """一条亚让变动：主队赔率 + 让球盘口 + 客队赔率。"""

    home_odds: Optional[float]
    home_odds_movement: Optional[Movement]
    handicap_raw: Optional[str]
    handicap_value: Optional[float]
    handicap_movement: Optional[Movement]
    away_odds: Optional[float]
    away_odds_movement: Optional[Movement]


@dataclass(frozen=True)
class OneXTwoChange(OddsChange):
    """一条胜平负变动：主胜 + 和局 + 客胜。"""

    home_win_odds: Optional[float]
    home_win_odds_movement: Optional[Movement]
    draw_odds: Optional[float]
    draw_odds_movement: Optional[Movement]
    away_win_odds: Optional[float]
    away_win_odds_movement: Optional[Movement]


@dataclass(frozen=True)
class OverUnderChange(OddsChange):
    """一条进球数变动：大球赔率 + 总进球盘口 + 小球赔率。"""

    over_odds: Optional[float]
    over_odds_movement: Optional[Movement]
    total_line_raw: Optional[str]
    total_line_value: Optional[float]
    total_line_movement: Optional[Movement]
    under_odds: Optional[float]
    under_odds_movement: Optional[Movement]


@dataclass(frozen=True)
class OddsMarketRequest:
    """一个可独立抓取、保存和重试的机构市场页面。"""

    company_id: int
    market: str


@dataclass(frozen=True)
class OddsMarketResult:
    """一个机构市场页面的采集结果；空市场也属于成功。"""

    request: OddsMarketRequest
    succeeded: bool
    error: Optional[str] = None


@dataclass(frozen=True)
class OddsSnapshot:
    """一次抓取的赔率快照，按机构市场记录每个页面的成败。"""

    match_id: int
    companies: Dict[int, str]
    handicap_changes: List[HandicapChange]
    one_x_two_changes: List[OneXTwoChange]
    over_under_changes: List[OverUnderChange]
    market_results: List[OddsMarketResult] = field(default_factory=list)

    @property
    def successful_markets(self) -> Tuple[OddsMarketRequest, ...]:
        """返回成功页面；兼容旧调用者构造的完整公司快照。"""

        if not self.market_results:
            return tuple(
                OddsMarketRequest(company_id, market)
                for company_id in self.companies
                for market in ("handicap", "one_x_two", "over_under")
            )
        return tuple(
            result.request for result in self.market_results if result.succeeded
        )

    @property
    def failed_markets(self) -> Dict[OddsMarketRequest, str]:
        return {
            result.request: result.error or "未知错误"
            for result in self.market_results
            if not result.succeeded
        }
