import unittest

from fetch_data.providers.titan007_detail import Titan007MatchDetailProvider


class Titan007MatchDetailProviderTests(unittest.TestCase):
    def test_default_url_uses_crown_simplified_chinese_page(self) -> None:
        self.assertEqual(
            Titan007MatchDetailProvider.DEFAULT_URL_TEMPLATE,
            "https://live.titan007.com/detail/{match_id}sb.htm",
        )

    def test_parse_finished_match_detail(self) -> None:
        detail = Titan007MatchDetailProvider.parse_detail(
            {
                "matchId": 3020831,
                "league": "女东南锦",
                "homeTeam": "新加坡女足",
                "awayTeam": "老挝女足",
                "scheduledTime": "2026-07-13 20:45",
                "scores": ["1", "1"],
                "statusText": "完",
            }
        )

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.match_id, 3020831)
        self.assertEqual(detail.league, "女东南锦")
        self.assertEqual(detail.home_team, "新加坡女足")
        self.assertEqual(detail.away_team, "老挝女足")
        self.assertEqual(detail.scheduled_time, "2026-07-13 20:45")
        self.assertEqual(detail.home_score, 1)
        self.assertEqual(detail.away_score, 1)
        self.assertEqual(detail.status_text, "完")

    def test_parse_scheduled_match_without_score_or_status(self) -> None:
        detail = Titan007MatchDetailProvider.parse_detail(
            {
                "matchId": "3021895",
                "league": "俄甲",
                "homeTeam": "切里宾斯克",
                "awayTeam": "SKA哈巴罗夫斯克",
                "scheduledTime": "2026-07-14 22:00",
                "scores": [],
                "statusText": "",
            }
        )

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIsNone(detail.home_score)
        self.assertIsNone(detail.away_score)
        self.assertEqual(detail.status_text, "未开始")

    def test_missing_required_fields_are_ignored(self) -> None:
        detail = Titan007MatchDetailProvider.parse_detail(
            {
                "matchId": 123,
                "league": "测试联赛",
                "homeTeam": "",
                "awayTeam": "客队",
                "scheduledTime": "2026-07-13 20:00",
                "scores": ["0", "0"],
                "statusText": "完",
            }
        )

        self.assertIsNone(detail)


if __name__ == "__main__":
    unittest.main()
