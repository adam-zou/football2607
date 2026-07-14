import json
import unittest

from fetch_data.models import HandicapChange, Movement, OneXTwoChange, OverUnderChange
from fetch_data.providers.titan007_odds import Titan007OddsProvider


def cell(text: str, color: str = "", col_span: int = 1):
    return {"text": text, "color": color, "colSpan": col_span}


class Titan007OddsProviderTests(unittest.TestCase):
    def test_missing_market_table_is_represented_by_no_rows(self) -> None:
        self.assertEqual(
            Titan007OddsProvider.parse_rows(
                "handicap", [], match_id=3020831, company_id=4
            ),
            [],
        )

    def test_build_url_changes_match_company_and_market(self) -> None:
        self.assertEqual(
            Titan007OddsProvider.build_url(3020831, 3, "over_under"),
            "https://vip.titan007.com/changeDetail/overunder.aspx"
            "?id=3020831&companyid=3&l=0",
        )

    def test_parse_over_under_preserves_dom_order_and_reverses_seq(self) -> None:
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

        changes = Titan007OddsProvider.parse_rows(
            "over_under", rows, match_id=3020831, company_id=3
        )

        self.assertEqual([change.seq for change in changes], [2, 1])
        self.assertEqual([change.change_time for change in changes], [
            "7-13 22:20",
            "7-13 22:20",
        ])
        latest = changes[0]
        self.assertIsInstance(latest, OverUnderChange)
        assert isinstance(latest, OverUnderChange)
        self.assertEqual(latest.home_score, 1)
        self.assertEqual(latest.away_score, 1)
        self.assertEqual(latest.over_odds_movement, Movement.UP)
        self.assertEqual(latest.total_line_movement, Movement.UNCHANGED)
        self.assertEqual(latest.under_odds_movement, Movement.DOWN)
        self.assertEqual(changes[1].total_line_value, 2.25)

    def test_parse_suspended_handicap_sets_market_values_to_null(self) -> None:
        changes = Titan007OddsProvider.parse_rows(
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
        )

        change = changes[0]
        self.assertIsInstance(change, HandicapChange)
        assert isinstance(change, HandicapChange)
        self.assertTrue(change.is_suspended)
        self.assertIsNone(change.home_odds)
        self.assertIsNone(change.home_odds_movement)
        self.assertIsNone(change.handicap_raw)
        self.assertIsNone(change.handicap_value)
        self.assertIsNone(change.handicap_movement)
        self.assertIsNone(change.away_odds)
        self.assertIsNone(change.away_odds_movement)

    def test_parse_prematch_one_x_two_uses_null_time_and_scores(self) -> None:
        changes = Titan007OddsProvider.parse_rows(
            "one_x_two",
            [
                {
                    "cells": [
                        cell(""),
                        cell(""),
                        cell("2.10"),
                        cell("2.80"),
                        cell("3.50"),
                        cell("07-12 06:54"),
                        cell("(初盘)"),
                    ]
                }
            ],
            match_id=3020831,
            company_id=8,
        )

        change = changes[0]
        self.assertIsInstance(change, OneXTwoChange)
        assert isinstance(change, OneXTwoChange)
        self.assertIsNone(change.match_minute)
        self.assertIsNone(change.home_score)
        self.assertIsNone(change.away_score)
        self.assertEqual(change.change_time, "07-12 06:54")
        self.assertEqual(change.source_status, "(初盘)")

    def test_convert_handicap_and_total_lines(self) -> None:
        self.assertEqual(Titan007OddsProvider.parse_handicap_value("平手"), 0)
        self.assertEqual(Titan007OddsProvider.parse_handicap_value("半球/一球"), 0.75)
        self.assertEqual(Titan007OddsProvider.parse_handicap_value("受平手/半球"), -0.25)
        self.assertIsNone(Titan007OddsProvider.parse_handicap_value("未知盘口"))
        self.assertEqual(Titan007OddsProvider.parse_total_line_value("1/1.5"), 1.25)
        self.assertIsNone(Titan007OddsProvider.parse_total_line_value("未知盘口"))

    def test_model_output_is_json_serializable_with_chinese_movements(self) -> None:
        changes = Titan007OddsProvider.parse_rows(
            "over_under",
            [
                {
                    "cells": [
                        cell(""), cell(""), cell("0.90", "red"),
                        cell("2"), cell("0.90", "green"),
                        cell("7-12 21:43"), cell("即"),
                    ]
                }
            ],
            match_id=3020831,
            company_id=3,
        )

        encoded = json.dumps(changes[0].to_dict(), ensure_ascii=False)
        self.assertIn('"over_odds_movement": "上升"', encoded)
        self.assertIn('"under_odds_movement": "下降"', encoded)


if __name__ == "__main__":
    unittest.main()
