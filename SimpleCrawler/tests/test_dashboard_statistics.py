import unittest
from datetime import date, datetime, timezone

from simple_crawler.dashboard_statistics import (
    BACKLOG_SQL,
    MATCH_SUMMARY_SQL,
    PROBLEM_MATCHES_SQL,
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
                1,
                1,
                3,
                0,
                0,
                0,
                1,
            )
        if self._result == 2:
            return (
                230, 0, 0, 220, 3, 2, 1, 4, 8, 210, 10, 2, 7,
                0, 0, 7, 0, 0, 0, 1,
            )
        if self._result == 3:
            return 76, 3
        if self._result == 5:
            return 76, 9, 3, 14, datetime(2026, 7, 16, 8, tzinfo=timezone.utc)
        raise AssertionError(f"unexpected fetchone call {self._result}")

    def fetchall(self):
        if self._result == 4:
            return [(3, "handicap", 123), (3, "one_x_two", 45)]
        if self._result == 6:
            return [
                (
                    3000001,
                    "2026-07-17 20:00",
                    "测试联赛",
                    "主队",
                    "客队",
                    "完",
                    "未完成",
                    False,
                    False,
                    "页面超时",
                ),
                (3000002, None, None, None, None, None, "未完成", True, False, None),
            ]
        raise AssertionError(f"unexpected fetchall call {self._result}")


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
        self.assertEqual(statistics["unfinished_not_started_count"], 1)
        self.assertEqual(statistics["unfinished_in_progress_count"], 1)
        self.assertEqual(statistics["unfinished_finished_count"], 3)
        self.assertEqual(statistics["unfinished_other_status_count"], 1)
        self.assertEqual(
            sum(
                statistics[key]
                for key in (
                    "unfinished_not_started_count",
                    "unfinished_in_progress_count",
                    "unfinished_finished_count",
                    "unfinished_postponed_count",
                    "unfinished_cancelled_count",
                    "unfinished_pending_count",
                    "unfinished_other_status_count",
                )
            ),
            statistics["crawl_unfinished_count"],
        )
        self.assertEqual(statistics["historical_match_count"], 230)
        self.assertEqual(statistics["historical_unfinished_count"], 8)
        self.assertEqual(statistics["historical_finished_count"], 220)
        self.assertEqual(statistics["historical_completed_count"], 210)
        self.assertEqual(statistics["historical_unfinished_finished_count"], 7)
        self.assertEqual(statistics["historical_unfinished_other_status_count"], 1)
        self.assertEqual(
            sum(
                statistics[key]
                for key in (
                    "historical_unfinished_not_started_count",
                    "historical_unfinished_in_progress_count",
                    "historical_unfinished_finished_count",
                    "historical_unfinished_postponed_count",
                    "historical_unfinished_cancelled_count",
                    "historical_unfinished_pending_count",
                    "historical_unfinished_other_status_count",
                )
            ),
            statistics["historical_unfinished_count"],
        )
        self.assertEqual(statistics["missing_details_count"], 76)
        self.assertEqual(statistics["invalid_scheduled_time_count"], 3)
        self.assertEqual(statistics["backlog"]["odds_match_count"], 9)
        self.assertEqual(statistics["backlog"]["final_page_count"], 14)
        self.assertEqual(
            statistics["backlog"]["oldest_pending_at"],
            "2026-07-16T08:00:00+00:00",
        )
        self.assertEqual(statistics["problem_matches"][0]["match_id"], 3000001)
        self.assertEqual(
            statistics["problem_matches"][0]["problems"],
            ["完场未完成", "页面采集失败"],
        )
        self.assertEqual(
            statistics["problem_matches"][1]["problems"],
            ["缺少详情"],
        )
        company_three = statistics["odds_counts"][0]
        self.assertEqual(company_three["handicap"], 123)
        self.assertEqual(company_three["one_x_two"], 45)
        self.assertEqual(company_three["over_under"], 0)
        self.assertEqual(len(statistics["odds_counts"]), 6)

    def test_match_states_are_identified_from_status_text(self) -> None:
        self.assertIn("status_text = '完'", MATCH_SUMMARY_SQL)
        self.assertIn("status_text IN ('上', '中', '下'", MATCH_SUMMARY_SQL)
        self.assertIn("status_text ~", MATCH_SUMMARY_SQL)
        self.assertIn("crawl_status = '未完成'", MATCH_SUMMARY_SQL)

    def test_backlog_matches_worker_eligibility_and_problem_scope(self) -> None:
        self.assertIn("details.updated_at > NOW() - INTERVAL '5 minutes'", BACKLOG_SQL)
        self.assertIn("final_success_at IS NULL", BACKLOG_SQL)
        self.assertIn("LIMIT 20", PROBLEM_MATCHES_SQL)
        self.assertIn("last_error IS NOT NULL", PROBLEM_MATCHES_SQL)


if __name__ == "__main__":
    unittest.main()
