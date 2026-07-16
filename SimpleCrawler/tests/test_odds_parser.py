import json
import unittest

from simple_crawler.models import (
    HandicapChange,
    Movement,
    OneXTwoChange,
    OverUnderChange,
)
from simple_crawler.odds_parser import Titan007OddsParser


def cell(text: str, color: str = "", col_span: int = 1):
    return {"text": text, "color": color, "colSpan": col_span}


class Titan007OddsParserTests(unittest.TestCase):
    def test_empty_market_has_no_changes(self) -> None:
        self.assertEqual(
            Titan007OddsParser.parse_rows(
                "handicap", [], match_id=3020831, company_id=4
            ),
            [],
        )

    def test_over_under_preserves_dom_order_and_reverses_seq(self) -> None:
        rows = [
            {
                "cells": [
                    cell("77"),
                    cell("1-1"),
                    cell("1.08", "red"),
                    cell("2.5"),
                    cell("0.73", "green"),
                    cell("7-13 22:20"),
                    cell("滚"),
                ]
            },
            {
                "cells": [
                    cell("77"),
                    cell("1-1"),
                    cell("1.05"),
                    cell("2/2.5", "red"),
                    cell("0.75"),
                    cell("7-13 22:20"),
                    cell("滚"),
                ]
            },
        ]

        changes = Titan007OddsParser.parse_rows(
            "over_under", rows, match_id=3020831, company_id=3
        )

        self.assertEqual([change.seq for change in changes], [2, 1])
        latest = changes[0]
        self.assertIsInstance(latest, OverUnderChange)
        assert isinstance(latest, OverUnderChange)
        self.assertEqual((latest.home_score, latest.away_score), (1, 1))
        self.assertEqual(latest.over_odds_movement, Movement.UP)
        self.assertEqual(latest.total_line_movement, Movement.UNCHANGED)
        self.assertEqual(latest.under_odds_movement, Movement.DOWN)
        self.assertEqual(changes[1].total_line_value, 2.25)

    def test_suspended_handicap_has_null_market_values(self) -> None:
        change = Titan007OddsParser.parse_rows(
            "handicap",
            [
                {
                    "cells": [
                        cell("14"),
                        cell("0-0"),
                        cell("封", "green", 3),
                        cell("7-13 20:59"),
                        cell("滚"),
                    ]
                }
            ],
            match_id=3020831,
            company_id=8,
        )[0]

        self.assertIsInstance(change, HandicapChange)
        assert isinstance(change, HandicapChange)
        self.assertTrue(change.is_suspended)
        self.assertIsNone(change.home_odds)
        self.assertIsNone(change.handicap_value)
        self.assertIsNone(change.away_odds)

    def test_compact_rows_have_no_match_time_or_score(self) -> None:
        cases = (
            (
                "handicap",
                [cell("0.90"), cell("半球"), cell("1.00"), cell("02:19"), cell("滚")],
                HandicapChange,
            ),
            (
                "one_x_two",
                [cell("1.01"), cell("46.00"), cell("81.00"), cell("02:19"), cell("滚")],
                OneXTwoChange,
            ),
            (
                "over_under",
                [cell("0.22"), cell("2.5"), cell("2.75"), cell("00:32"), cell("即")],
                OverUnderChange,
            ),
        )

        for market, cells, expected_type in cases:
            with self.subTest(market=market):
                change = Titan007OddsParser.parse_rows(
                    market,
                    [{"cells": cells}],
                    match_id=2931262,
                    company_id=4,
                )[0]
                self.assertIsInstance(change, expected_type)
                self.assertIsNone(change.match_minute)
                self.assertIsNone(change.home_score)
                self.assertIsNone(change.away_score)

    def test_converts_handicap_and_total_lines(self) -> None:
        self.assertEqual(Titan007OddsParser.parse_handicap_value("平手"), 0)
        self.assertEqual(
            Titan007OddsParser.parse_handicap_value("半球/一球"), 0.75
        )
        self.assertEqual(
            Titan007OddsParser.parse_handicap_value("受平手/半球"), -0.25
        )
        self.assertIsNone(
            Titan007OddsParser.parse_handicap_value("未知盘口")
        )
        self.assertEqual(
            Titan007OddsParser.parse_total_line_value("1/1.5"), 1.25
        )

    def test_model_is_json_serializable_with_chinese_movements(self) -> None:
        change = Titan007OddsParser.parse_rows(
            "over_under",
            [
                {
                    "cells": [
                        cell(""),
                        cell(""),
                        cell("0.90", "red"),
                        cell("2"),
                        cell("0.90", "green"),
                        cell("7-12 21:43"),
                        cell("即"),
                    ]
                }
            ],
            match_id=3020831,
            company_id=3,
        )[0]

        encoded = json.dumps(change.to_dict(), ensure_ascii=False)
        self.assertIn('"over_odds_movement": "上升"', encoded)
        self.assertIn('"under_odds_movement": "下降"', encoded)


if __name__ == "__main__":
    unittest.main()
