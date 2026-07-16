import unittest
from pathlib import Path
from unittest.mock import patch

from fetch_data.models import MatchBasicInfo
from fetch_data.postgres import (
    FETCH_PENDING_DYNAMIC_IDS,
    FETCH_PENDING_DETAIL_IDS,
    INITIALIZE_MATCH_STATUS_TABLE,
    RECORD_DYNAMIC_FAILURE,
    RECORD_DYNAMIC_SUCCESS,
    parse_scheduled_at,
)


class FakeCursor:
    def __init__(self) -> None:
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))

    def fetchone(self):
        return ("match_status", "match_basic_info")


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.cursor_instance


def detail(status_text: str = "未开始") -> MatchBasicInfo:
    return MatchBasicInfo(
        source="titan007",
        match_id=3020831,
        league="测试联赛",
        home_team="主队",
        away_team="客队",
        scheduled_time="2026-07-14 20:00",
        home_score=1 if status_text == "完" else None,
        away_score=0 if status_text == "完" else None,
        status_text=status_text,
    )


class PostgresMatchStoreTests(unittest.TestCase):
    def test_scheduled_time_is_also_normalized_to_timestamptz(self) -> None:
        scheduled_at = parse_scheduled_at("2026-07-14 20:00")

        self.assertIsNotNone(scheduled_at)
        assert scheduled_at is not None
        self.assertEqual(scheduled_at.isoformat(), "2026-07-14T20:00:00+08:00")
        self.assertIsNone(parse_scheduled_at("20:00"))

    def test_match_basic_info_migration_contains_scheduled_at(self) -> None:
        migration = (
            Path(__file__).parents[1]
            / "fetch_data"
            / "migrations"
            / "002_match_basic_info.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("scheduled_at TIMESTAMPTZ", migration)
        self.assertIn("scheduled_at TIMESTAMPTZ", INITIALIZE_MATCH_STATUS_TABLE)

    def test_detail_and_dynamic_status_have_independent_queues(self) -> None:
        self.assertIn("detail_status = '未完成'", FETCH_PENDING_DETAIL_IDS)
        self.assertNotIn("crawl_status = '未完成'", FETCH_PENDING_DETAIL_IDS)

        self.assertIn("detail_status = '已完成'", FETCH_PENDING_DYNAMIC_IDS)
        self.assertIn("crawl_status = '未完成'", FETCH_PENDING_DYNAMIC_IDS)
        self.assertIn("INTERVAL '24 hours'", FETCH_PENDING_DYNAMIC_IDS)
        self.assertIn("next_attempt_at", FETCH_PENDING_DYNAMIC_IDS)

    def test_migrations_define_detail_and_dynamic_tracking(self) -> None:
        self.assertIn("detail_status TEXT", INITIALIZE_MATCH_STATUS_TABLE)
        self.assertIn("dynamic_updated_at TIMESTAMPTZ", INITIALIZE_MATCH_STATUS_TABLE)
        self.assertIn("CREATE TABLE IF NOT EXISTS match_dynamic_schedule", INITIALIZE_MATCH_STATUS_TABLE)
        self.assertIn("INTERVAL '8 hours'", RECORD_DYNAMIC_SUCCESS)
        self.assertIn("INTERVAL '3 hours'", RECORD_DYNAMIC_FAILURE)

    def test_successful_detail_write_marks_detail_status_completed(self) -> None:
        from fetch_data.postgres import PostgresMatchStore

        store = PostgresMatchStore("postgresql://example/football")
        store._connection = FakeConnection()

        with patch("fetch_data.postgres.execute_values"):
            store._upsert_match_details_sync([detail()])

        statements = [
            statement
            for statement, _ in store._connection.cursor_instance.executions
        ]
        self.assertTrue(
            any("detail_status = '已完成'" in statement for statement in statements)
        )

    def test_dynamic_write_updates_current_fields_and_schedules_success(self) -> None:
        from fetch_data.postgres import PostgresMatchStore

        store = PostgresMatchStore("postgresql://example/football")
        store._connection = FakeConnection()

        with patch("fetch_data.postgres.execute_values") as execute:
            store._upsert_match_dynamics_sync([detail("完")])

        dynamic_statement = execute.call_args.args[1]
        self.assertIn("scheduled_time = dynamic.scheduled_time", dynamic_statement)
        self.assertIn("status_text = dynamic.status_text", dynamic_statement)
        self.assertIn("dynamic_updated_at = NOW()", dynamic_statement)
        statements = [
            statement
            for statement, _ in store._connection.cursor_instance.executions
        ]
        self.assertIn(RECORD_DYNAMIC_SUCCESS, statements)


if __name__ == "__main__":
    unittest.main()
