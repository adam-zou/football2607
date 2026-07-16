"""Convert Titan007 odds table rows into SimpleCrawler's stable models."""

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .models import (
    HandicapChange,
    Movement,
    OneXTwoChange,
    OverUnderChange,
)
from .companies import COMPANY_NAMES


RawCell = Dict[str, Any]
RawRow = Dict[str, Any]
MarketChange = Union[HandicapChange, OneXTwoChange, OverUnderChange]


class Titan007OddsParser:
    """Parse the three supported Titan007 odds-change market tables."""

    MARKETS = {"handicap", "one_x_two", "over_under"}
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

    @classmethod
    def parse_rows(
        cls,
        market: str,
        rows: Sequence[RawRow],
        *,
        match_id: int,
        company_id: int,
    ) -> List[MarketChange]:
        """Parse DOM-ordered rows and assign oldest-first stable sequences."""

        if market not in cls.MARKETS:
            raise ValueError(f"unsupported market: {market}")
        cls._validate_match_id(match_id)
        if company_id not in COMPANY_NAMES:
            raise ValueError(f"unsupported company_id: {company_id}")

        total_rows = len(rows)
        changes: List[MarketChange] = []
        for dom_index, row in enumerate(rows):
            cells = row.get("cells")
            if not isinstance(cells, list):
                raise ValueError(f"row {dom_index + 1} has no cells")
            changes.append(
                cls._parse_row(
                    market,
                    cells,
                    match_id=match_id,
                    company_id=company_id,
                    seq=total_rows - dom_index,
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
        suspended = cls._is_suspended(cells)
        compact = len(cells) == 5 and not suspended
        if len(cells) not in {5, 7}:
            raise ValueError(
                f"unexpected {market} row shape: {len(cells)} cells"
            )

        if compact:
            match_minute = None
            home_score, away_score = None, None
            market_index = 0
            change_index = 3
            status_index = 4
        else:
            match_minute = cls._parse_match_minute(cls._text(cells[0]))
            home_score, away_score = cls._parse_score(cls._text(cells[1]))
            market_index = 2
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
            return cls._suspended_change(market, common)
        if market == "handicap":
            handicap_raw = cls._text(cells[market_index + 1])
            return HandicapChange(
                **common,
                home_odds=cls._parse_float(cls._text(cells[market_index])),
                home_odds_movement=cls._movement(cells[market_index]),
                handicap_raw=handicap_raw,
                handicap_value=cls.parse_handicap_value(handicap_raw),
                handicap_movement=cls._movement(cells[market_index + 1]),
                away_odds=cls._parse_float(cls._text(cells[market_index + 2])),
                away_odds_movement=cls._movement(cells[market_index + 2]),
            )
        if market == "one_x_two":
            return OneXTwoChange(
                **common,
                home_win_odds=cls._parse_float(cls._text(cells[market_index])),
                home_win_odds_movement=cls._movement(cells[market_index]),
                draw_odds=cls._parse_float(cls._text(cells[market_index + 1])),
                draw_odds_movement=cls._movement(cells[market_index + 1]),
                away_win_odds=cls._parse_float(
                    cls._text(cells[market_index + 2])
                ),
                away_win_odds_movement=cls._movement(cells[market_index + 2]),
            )

        total_line_raw = cls._text(cells[market_index + 1])
        return OverUnderChange(
            **common,
            over_odds=cls._parse_float(cls._text(cells[market_index])),
            over_odds_movement=cls._movement(cells[market_index]),
            total_line_raw=total_line_raw,
            total_line_value=cls.parse_total_line_value(total_line_raw),
            total_line_movement=cls._movement(cells[market_index + 1]),
            under_odds=cls._parse_float(cls._text(cells[market_index + 2])),
            under_odds_movement=cls._movement(cells[market_index + 2]),
        )

    @staticmethod
    def _suspended_change(
        market: str,
        common: Dict[str, Any],
    ) -> MarketChange:
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
        normalized = re.sub(r"\s+", "", value or "")
        if not normalized:
            return None
        negative = False
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
        color = str(cell.get("color") or "").strip().lower()
        if color == "red":
            return Movement.UP
        if color == "green":
            return Movement.DOWN
        return Movement.UNCHANGED

    @staticmethod
    def _is_suspended(cells: Sequence[RawCell]) -> bool:
        return (
            len(cells) == 5
            and Titan007OddsParser._text(cells[2]) == "封"
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
