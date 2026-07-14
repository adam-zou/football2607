import unittest

from fetch_data.models import MatchStatus
from fetch_data.providers.titan007 import Titan007Provider


class Titan007ProviderTests(unittest.TestCase):
    def test_parse_live_match_row(self) -> None:
        match = Titan007Provider.parse_row(
            {
                "rowId": "tr1_2978276",
                "scheduledTime": "2026-07-14 19:35",
                "cells": [
                    "",
                    "闽超",
                    "19:35",
                    "90+1",
                    "莆田队",
                    "2-0",
                    "三明队",
                    "1-0",
                ],
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.match_id, "2978276")
        self.assertEqual(match.league, "闽超")
        self.assertEqual(match.home_team, "莆田队")
        self.assertEqual(match.away_team, "三明队")
        self.assertEqual(match.score, "2-0")
        self.assertEqual(match.home_score, 2)
        self.assertEqual(match.away_score, 0)
        self.assertIs(match.status, MatchStatus.LIVE)
        self.assertEqual(match.status_text, "90+1")
        self.assertEqual(match.scheduled_time, "2026-07-14 19:35")

    def test_parse_scheduled_match_without_score(self) -> None:
        match = Titan007Provider.parse_row(
            {
                "rowId": "tr1_3021895",
                "scheduledTime": "2026-07-14 22:00",
                "cells": [
                    "",
                    "俄甲",
                    "22:00",
                    "",
                    "切亚宾斯克",
                    "-",
                    "SKA哈巴罗夫斯克",
                ],
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertIsNone(match.score)
        self.assertIsNone(match.home_score)
        self.assertIsNone(match.away_score)
        self.assertIs(match.status, MatchStatus.SCHEDULED)
        self.assertEqual(match.scheduled_time, "2026-07-14 22:00")

    def test_invalid_and_duplicate_rows_are_ignored(self) -> None:
        valid = {
            "rowId": "tr1_123",
            "scheduledTime": "2026-07-14 20:00",
            "cells": ["", "英超", "20:00", "完", "主队", "3-1", "客队"],
        }
        matches = Titan007Provider.parse_rows(
            [valid, valid, {"rowId": "advert", "cells": ["推广"]}]
        )

        self.assertEqual(len(matches), 1)
        self.assertIs(matches[0].status, MatchStatus.FINISHED)

    def test_unknown_status_preserves_original_text(self) -> None:
        match = Titan007Provider.parse_row(
            {
                "rowId": "tr1_456",
                "scheduledTime": "2026-07-14 20:00",
                "cells": ["", "测试联赛", "20:00", "待定", "主队", "-", "客队"],
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertIs(match.status, MatchStatus.UNKNOWN)
        self.assertEqual(match.status_text, "待定")

    def test_row_without_full_scheduled_time_is_ignored(self) -> None:
        match = Titan007Provider.parse_row(
            {
                "rowId": "tr1_789",
                "scheduledTime": "20:00",
                "cells": ["", "测试联赛", "20:00", "", "主队", "-", "客队"],
            }
        )

        self.assertIsNone(match)


if __name__ == "__main__":
    unittest.main()
