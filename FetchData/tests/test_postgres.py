import unittest
from pathlib import Path

from fetch_data.postgres import INITIALIZE_MATCH_STATUS_TABLE, parse_scheduled_at


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


if __name__ == "__main__":
    unittest.main()
