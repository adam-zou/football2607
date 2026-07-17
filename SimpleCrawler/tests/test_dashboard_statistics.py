import unittest
from datetime import date

from simple_crawler.dashboard_statistics import (
    MATCH_SUMMARY_SQL,
    collect_daily_statistics,
)


class FakeCursor:
    def __init__(self):
        self.statements = []
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def execute(self, statement):
        self.statements.append(statement)
        self._result = len(self.statements)

    def fetchone(self):
        if self._result == 1:
            return (
                date(2026, 7, 17),
                12,
                2,
                1,
                7,
                1,
                0,
                0,
                1,
                6,
                4,
                1,
                1,
                3,
            )
        if self._result == 2:
            return 230, 0, 0, 220, 3, 2, 1, 4, 8, 210, 10, 2, 7
        return 76, 3

    def fetchall(self):
        return [(3, "handicap", 123), (3, "one_x_two", 45)]


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()

    def cursor(self):
        return self.cursor_instance


class DashboardStatisticsTests(unittest.TestCase):
    def test_collects_match_and_company_market_counts(self) -> None:
        connection = FakeConnection()

        statistics = collect_daily_statistics(connection)

        self.assertEqual(statistics["date"], "2026-07-17")
        self.assertEqual(statistics["match_count"], 12)
        self.assertEqual(statistics["finished_count"], 7)
        self.assertEqual(statistics["in_progress_count"], 1)
        self.assertEqual(statistics["not_started_count"], 2)
        self.assertEqual(statistics["crawl_completed_count"], 4)
        self.assertEqual(statistics["abnormal_count"], 1)
        self.assertEqual(statistics["paused_count"], 1)
        self.assertEqual(statistics["finished_unfinished_count"], 3)
        self.assertEqual(statistics["historical_match_count"], 230)
        self.assertEqual(statistics["historical_unfinished_count"], 8)
        self.assertEqual(statistics["historical_finished_count"], 220)
        self.assertEqual(statistics["historical_completed_count"], 210)
        self.assertEqual(statistics["missing_details_count"], 76)
        self.assertEqual(statistics["invalid_scheduled_time_count"], 3)
        company_three = statistics["odds_counts"][0]
        self.assertEqual(company_three["handicap"], 123)
        self.assertEqual(company_three["one_x_two"], 45)
        self.assertEqual(company_three["over_under"], 0)
        self.assertEqual(len(statistics["odds_counts"]), 6)

    def test_match_states_are_identified_from_status_text(self) -> None:
        self.assertIn("status_text = '完'", MATCH_SUMMARY_SQL)
        self.assertIn("status_text IN ('上', '中', '下'", MATCH_SUMMARY_SQL)
        self.assertIn("status_text ~", MATCH_SUMMARY_SQL)


if __name__ == "__main__":
    unittest.main()
