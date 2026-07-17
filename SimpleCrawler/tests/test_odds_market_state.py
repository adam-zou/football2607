import unittest
from decimal import Decimal

from odds_market_state import (
    CREATE_ODDS_MARKET_STATE_TABLE_SQL,
    final_snapshot_complete,
    load_pending_final_pages,
    prepare_final_snapshot,
    record_market_result,
)


class FakeCursor:
    def __init__(self, rows=()) -> None:
        self.executions = []
        self.rows = list(rows)

    def execute(self, statement, parameters=None) -> None:
        self.executions.append((statement, parameters))

    def executemany(self, statement, parameters) -> None:
        self.executions.append((statement, list(parameters)))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class OddsMarketStateTests(unittest.TestCase):
    def test_schema_has_one_row_per_match_company_and_market(self) -> None:
        self.assertIn(
            "PRIMARY KEY (match_id, company_id, market)",
            CREATE_ODDS_MARKET_STATE_TABLE_SQL,
        )
        self.assertIn(
            "last_attempt_at TIMESTAMPTZ",
            CREATE_ODDS_MARKET_STATE_TABLE_SQL,
        )
        self.assertIn(
            "last_success_at TIMESTAMPTZ",
            CREATE_ODDS_MARKET_STATE_TABLE_SQL,
        )
        self.assertIn("row_count INTEGER", CREATE_ODDS_MARKET_STATE_TABLE_SQL)
        self.assertIn("content_hash CHAR(64)", CREATE_ODDS_MARKET_STATE_TABLE_SQL)
        self.assertIn("last_error TEXT", CREATE_ODDS_MARKET_STATE_TABLE_SQL)
        self.assertIn("final_required BOOLEAN", CREATE_ODDS_MARKET_STATE_TABLE_SQL)
        self.assertIn(
            "final_success_at TIMESTAMPTZ",
            CREATE_ODDS_MARKET_STATE_TABLE_SQL,
        )
        self.assertIn("待抓取", CREATE_ODDS_MARKET_STATE_TABLE_SQL)

    def test_success_records_count_and_deterministic_content_hash(self) -> None:
        rows = [(1, Decimal("0.880"), None), (2, Decimal("0.910"), "上升")]
        first_cursor = FakeCursor()
        second_cursor = FakeCursor()

        first_hash = record_market_result(
            first_cursor,
            match_id=123,
            company_id=3,
            market="handicap",
            rows=rows,
            final=False,
        )
        second_hash = record_market_result(
            second_cursor,
            match_id=123,
            company_id=3,
            market="handicap",
            rows=rows,
            final=False,
        )

        self.assertEqual(first_hash, second_hash)
        self.assertEqual(len(first_hash), 64)
        statement, parameters = first_cursor.executions[-1]
        self.assertIn("last_success_at", statement)
        self.assertEqual(parameters[:4], (123, 3, "handicap", 2))
        self.assertEqual(parameters[4], first_hash)

    def test_failure_records_error_without_replacing_last_success(self) -> None:
        cursor = FakeCursor()

        content_hash = record_market_result(
            cursor,
            match_id=123,
            company_id=3,
            market="over_under",
            error="navigation timeout",
            final=False,
        )

        self.assertIsNone(content_hash)
        statement, parameters = cursor.executions[-1]
        self.assertIn("fetch_status = '失败'", statement)
        self.assertNotIn("last_success_at =", statement)
        self.assertNotIn("row_count =", statement)
        self.assertEqual(
            parameters,
            (123, 3, "over_under", "navigation timeout"),
        )

    def test_final_success_clears_requirement_and_sets_success_time(self) -> None:
        cursor = FakeCursor()

        record_market_result(
            cursor,
            match_id=123,
            company_id=3,
            market="handicap",
            rows=[],
            final=True,
        )

        statement, _ = cursor.executions[-1]
        self.assertIn("final_required = FALSE", statement)
        self.assertIn("final_success_at = NOW()", statement)

    def test_prepares_and_loads_only_pending_final_pages(self) -> None:
        cursor = FakeCursor([(3, "handicap"), (4, "over_under")])

        prepare_final_snapshot(
            cursor,
            match_id=123,
            company_ids=[3, 4],
            markets=["handicap", "over_under"],
        )
        pages = load_pending_final_pages(cursor, 123, [3, 4])

        prepare_statement, prepared = cursor.executions[0]
        self.assertIn("final_required", prepare_statement)
        self.assertEqual(len(prepared), 4)
        self.assertEqual(
            pages,
            [(3, "handicap"), (4, "over_under")],
        )

    def test_final_snapshot_requires_every_configured_page(self) -> None:
        self.assertTrue(
            final_snapshot_complete(
                FakeCursor([(18, True)]),
                123,
                [3, 4, 8, 24, 31, 47],
                3,
            )
        )
        self.assertFalse(
            final_snapshot_complete(
                FakeCursor([(17, True)]),
                123,
                [3, 4, 8, 24, 31, 47],
                3,
            )
        )

    def test_requires_exactly_one_of_rows_or_error(self) -> None:
        cursor = FakeCursor()

        with self.assertRaises(ValueError):
            record_market_result(
                cursor,
                match_id=123,
                company_id=3,
                market="one_x_two",
                final=False,
            )
        with self.assertRaises(ValueError):
            record_market_result(
                cursor,
                match_id=123,
                company_id=3,
                market="one_x_two",
                rows=[],
                error="unexpected",
                final=False,
            )


if __name__ == "__main__":
    unittest.main()
