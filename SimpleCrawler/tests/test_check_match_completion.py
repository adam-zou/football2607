import sys
import unittest
from unittest.mock import patch

from check_match_completion import (
    DETAIL_SCRIPT,
    ODDS_SCRIPT,
    final_status_for_refresh,
    is_match_finished,
    mark_final_status,
    refresh_detail_once,
    refresh_odds_once,
    select_pending_matches,
)


class FakeCursor:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.statement = None
        self.parameters = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def execute(self, statement, parameters=()) -> None:
        self.statement = statement
        self.parameters = parameters

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, rows=()) -> None:
        self.cursor_instance = FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1


class PendingMatchTests(unittest.TestCase):
    @patch("check_match_completion.subprocess.run")
    def test_detail_refresh_calls_detail_script_for_only_that_match(self, run) -> None:
        run.return_value.returncode = 0

        returncode = refresh_detail_once(123456)

        self.assertEqual(returncode, 0)
        run.assert_called_once_with(
            [sys.executable, str(DETAIL_SCRIPT), "123456"],
            check=False,
        )

    def test_rechecks_finished_status_after_detail_refresh(self) -> None:
        connection = FakeConnection([(True,)])

        self.assertTrue(is_match_finished(connection, 123456))
        self.assertIn(
            "status_text = '完'",
            connection.cursor_instance.statement,
        )
        self.assertEqual(
            connection.cursor_instance.parameters,
            (123456,),
        )

    def test_missing_detail_is_not_finished(self) -> None:
        self.assertFalse(is_match_finished(FakeConnection(), 123456))

    def test_selects_finished_before_overdue_unfinished_matches(self) -> None:
        connection = FakeConnection([(1001, True), (1002, False)])

        matches = select_pending_matches(connection, 25, ["未完成"])

        self.assertEqual(matches, [(1001, True), (1002, False)])
        statement = connection.cursor_instance.statement
        self.assertIn("details.status_text = '完'", statement)
        self.assertIn("details.status_text <> '完'", statement)
        self.assertIn("INTERVAL '4 hours'", statement)
        self.assertIn("ids.crawl_status = ANY(%s)", statement)
        self.assertIn(
            "CASE WHEN details.status_text = '完' THEN 0 ELSE 1 END",
            statement,
        )
        self.assertEqual(
            connection.cursor_instance.parameters,
            (["未完成"], 25),
        )

    @patch("check_match_completion.subprocess.run")
    def test_final_refresh_calls_odds_script_for_only_that_match(self, run) -> None:
        run.return_value.returncode = 7

        returncode = refresh_odds_once(123456)

        self.assertEqual(returncode, 7)
        run.assert_called_once_with(
            [sys.executable, str(ODDS_SCRIPT), "123456"],
            check=False,
        )

    def test_mark_final_status_updates_status_and_commits(self) -> None:
        connection = FakeConnection()

        mark_final_status(connection, 123456, "异常")

        self.assertIn(
            "SET crawl_status = %s",
            connection.cursor_instance.statement,
        )
        self.assertEqual(
            connection.cursor_instance.parameters,
            ("异常", 123456),
        )
        self.assertEqual(connection.commits, 1)

    def test_only_majority_failure_becomes_abnormal(self) -> None:
        self.assertEqual(final_status_for_refresh(0), "暂停爬取")
        self.assertEqual(final_status_for_refresh(1), "暂停爬取")
        self.assertEqual(final_status_for_refresh(10), "异常")
        self.assertIsNone(final_status_for_refresh(2))
        self.assertIsNone(final_status_for_refresh(3))
        self.assertIsNone(final_status_for_refresh(-9))


if __name__ == "__main__":
    unittest.main()
