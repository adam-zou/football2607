"""Stable odds values produced by the standalone crawler parser."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional


class Movement(str, Enum):
    """赔率或盘口相对上一条历史记录的变化方向。"""

    UP = "上升"
    DOWN = "下降"
    UNCHANGED = "不变"


@dataclass(frozen=True)
class OddsChange:
    """三类赔率变动共有的字段。"""

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
    """一条亚让变动。"""

    home_odds: Optional[float]
    home_odds_movement: Optional[Movement]
    handicap_raw: Optional[str]
    handicap_value: Optional[float]
    handicap_movement: Optional[Movement]
    away_odds: Optional[float]
    away_odds_movement: Optional[Movement]


@dataclass(frozen=True)
class OneXTwoChange(OddsChange):
    """一条胜平负变动。"""

    home_win_odds: Optional[float]
    home_win_odds_movement: Optional[Movement]
    draw_odds: Optional[float]
    draw_odds_movement: Optional[Movement]
    away_win_odds: Optional[float]
    away_win_odds_movement: Optional[Movement]


@dataclass(frozen=True)
class OverUnderChange(OddsChange):
    """一条进球数变动。"""

    over_odds: Optional[float]
    over_odds_movement: Optional[Movement]
    total_line_raw: Optional[str]
    total_line_value: Optional[float]
    total_line_movement: Optional[Movement]
    under_odds: Optional[float]
    under_odds_movement: Optional[Movement]
