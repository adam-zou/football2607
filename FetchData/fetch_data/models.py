"""项目中各层共享的数据模型。

可以把这里理解成项目的“统一数据格式”：Provider 负责把网页内容转换成这些
对象，存储层和命令行层只消费对象，不需要知道网页 DOM 长什么样。
"""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class MatchStatus(str, Enum):
    """统一后的比赛状态，屏蔽不同数据源使用的不同原始文字。"""

    SCHEDULED = "scheduled"
    LIVE = "live"
    HALF_TIME = "half_time"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class Movement(str, Enum):
    """赔率或盘口相对上一条历史记录的变化方向。"""

    UP = "上升"
    DOWN = "下降"
    UNCHANGED = "不变"


@dataclass(frozen=True)
class Match:
    """从比赛列表页得到的一场比赛快照。

    ``frozen=True`` 表示对象创建后不能修改，避免异步任务之间意外篡改同一份
    抓取结果。``status_text`` 保留网页原文，``status`` 则方便程序统一判断。
    """

    source: str
    match_id: str
    league: str
    home_team: str
    away_team: str
    score: Optional[str]
    home_score: Optional[int]
    away_score: Optional[int]
    status: MatchStatus
    status_text: str
    scheduled_time: str

    def to_dict(self) -> Dict[str, Any]:
        """转换为可交给 ``json.dumps`` 的普通字典。"""

        return asdict(self)


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
class OddsSnapshot:
    """一次命令抓到的完整赔率快照，按三个市场分别保存。"""

    match_id: int
    companies: Dict[int, str]
    handicap_changes: List[HandicapChange]
    one_x_two_changes: List[OneXTwoChange]
    over_under_changes: List[OverUnderChange]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
