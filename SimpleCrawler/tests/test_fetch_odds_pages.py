import unittest
from unittest import mock

from fetch_odds_pages import (
    EXIT_INDETERMINATE,
    EXIT_MAJORITY_FAILURE,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    parse_args,
    result_exit_code,
    select_match_ids,
)
from odds_collection import (
    UPSERT_HANDICAP,
    UPSERT_ONE_X_TWO,
    UPSERT_OVER_UNDER,
    OddsPageJob,
    extract_page_rows_from_html,
    persist_market_page,
)


class OddsResultTests(unittest.TestCase):
    def test_all_success_is_success(self) -> None:
        self.assertEqual(result_exit_code(18, 0), EXIT_SUCCESS)

    def test_half_or_fewer_failures_is_partial(self) -> None:
        self.assertEqual(result_exit_code(10, 8), EXIT_PARTIAL_FAILURE)
        self.assertEqual(result_exit_code(9, 9), EXIT_PARTIAL_FAILURE)

    def test_more_than_half_failures_is_majority_failure(self) -> None:
        self.assertEqual(result_exit_code(8, 10), EXIT_MAJORITY_FAILURE)

    def test_indeterminate_exit_is_distinct_from_page_count_result(self) -> None:
        self.assertNotIn(
            EXIT_INDETERMINATE,
            (EXIT_SUCCESS, EXIT_PARTIAL_FAILURE, EXIT_MAJORITY_FAILURE),
        )


class OddsConcurrencyArgumentTests(unittest.TestCase):
    def test_defaults_to_twelve_workers(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            args = parse_args([])

        self.assertEqual(args.concurrency, 12)

    def test_environment_can_override_default(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"SIMPLE_CRAWLER_ODDS_PAGE_CONCURRENCY": "6"},
            clear=True,
        ):
            args = parse_args([])

        self.assertEqual(args.concurrency, 6)


class FakeCursor:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def execute(self, statement, parameters=None) -> None:
        self.executions.append((statement, parameters))

    def executemany(self, statement, parameters) -> None:
        self.executions.append((statement, parameters))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows) -> None:
        self.cursor_instance = FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1


class OddsMatchSelectionTests(unittest.TestCase):
    def test_database_selection_excludes_matches_over_thirty_minutes_away(
        self,
    ) -> None:
        connection = FakeConnection([(101,), (102,)])

        selected = select_match_ids(connection, [], None, ["未完成"])

        self.assertEqual(selected, [101, 102])
        statement, _ = connection.cursor_instance.executions[-1]
        self.assertIn("JOIN match_details AS details", statement)
        self.assertIn("AT TIME ZONE 'Asia/Shanghai'", statement)
        self.assertIn("NOW() + INTERVAL '30 minutes'", statement)
        self.assertIn("NOW() - INTERVAL '4 hours'", statement)
        self.assertIn("details.updated_at > NOW() - INTERVAL '5 minutes'", statement)
        self.assertIn("details.status_text <> '完'", statement)
        self.assertIn("details.status_text = '完'", statement)
        self.assertIn("THEN 0", statement)
        self.assertIn("THEN 1", statement)
        self.assertIn("THEN 2", statement)

    def test_explicit_ids_cannot_bypass_scheduled_time_filter(self) -> None:
        connection = FakeConnection([(102,)])

        selected = select_match_ids(
            connection,
            [101, 102],
            None,
            ["未完成"],
        )

        self.assertEqual(selected, [102])
        statement, _ = connection.cursor_instance.executions[-1]
        self.assertIn("JOIN match_details AS details", statement)
        self.assertIn("NOW() + INTERVAL '30 minutes'", statement)
        self.assertIn("NOW() - INTERVAL '4 hours'", statement)
        self.assertIn("details.updated_at > NOW() - INTERVAL '5 minutes'", statement)


class OddsPersistenceTests(unittest.TestCase):
    def test_upserts_skip_conflicts_whose_values_are_unchanged(self) -> None:
        for statement in (
            UPSERT_HANDICAP,
            UPSERT_ONE_X_TWO,
            UPSERT_OVER_UNDER,
        ):
            with self.subTest(statement=statement.splitlines()[1]):
                self.assertIn("WHERE (", statement)
                self.assertIn("IS DISTINCT FROM (", statement)
                self.assertIn("EXCLUDED.match_minute", statement)
                self.assertIn("EXCLUDED.source_status", statement)

    @mock.patch("odds_collection.change_values", return_value=(1, 2, 3))
    @mock.patch("odds_collection.execute_values")
    def test_writes_pages_in_batches_of_five_hundred(
        self,
        execute_values,
        _change_values,
    ) -> None:
        connection = FakeConnection([])

        persist_market_page(
            connection,
            OddsPageJob(123, 3, "handicap"),
            [mock.sentinel.change],
        )

        execute_values.assert_called_once_with(
            connection.cursor_instance,
            UPSERT_HANDICAP,
            [(1, 2, 3)],
            page_size=500,
        )
        self.assertEqual(connection.commits, 1)

    @mock.patch("odds_collection.record_market_result")
    @mock.patch("odds_collection.change_values", return_value=(1, 2, 3))
    @mock.patch("odds_collection.execute_values")
    def test_success_records_page_state_in_the_odds_transaction(
        self,
        _execute_values,
        _change_values,
        record_market_result,
    ) -> None:
        connection = FakeConnection([])

        persist_market_page(
            connection,
            OddsPageJob(123, 3, "handicap"),
            [mock.sentinel.change],
        )

        record_market_result.assert_called_once_with(
            connection.cursor_instance,
            match_id=123,
            company_id=3,
            market="handicap",
            rows=[(1, 2, 3)],
            final=False,
        )
        self.assertEqual(connection.commits, 1)


class OddsResponseHtmlTests(unittest.TestCase):
    def test_extracts_rows_from_server_rendered_market_html(self) -> None:
        html = """
            <html><body>
              <a href="handicap.aspx">亚让</a>
              <div id="odds2"><table>
                <tr><th>时间</th><th>赔率</th></tr>
                <tr>
                  <td> 10:30 </td>
                  <td colspan="2"><font color="red"> 0.88 </font></td>
                </tr>
              </table></div>
            </body></html>
        """

        rows = extract_page_rows_from_html(html, "#odds2 table")

        self.assertEqual(
            rows,
            [
                {
                    "cells": [
                        {"text": "10:30", "colSpan": 1, "color": ""},
                        {"text": "0.88", "colSpan": 2, "color": "red"},
                    ]
                }
            ],
        )

    def test_valid_market_shell_without_table_is_empty(self) -> None:
        html = '<html><body><div id="odds2"></div></body></html>'

        self.assertEqual(
            extract_page_rows_from_html(html, "#odds2 table"),
            [],
        )

    def test_repairs_titan007_cells_with_missing_end_tags(self) -> None:
        html = """
            <html><body><div id="odds2"><table>
              <tr><th>盘口</th><th>赔率</th></tr>
              <tr><td>平手<td><font color="green">1.66</font></tr>
            </table></div></body></html>
        """

        rows = extract_page_rows_from_html(html, "#odds2 table")

        self.assertEqual(
            rows[0]["cells"],
            [
                {"text": "平手", "colSpan": 1, "color": ""},
                {"text": "1.66", "colSpan": 1, "color": "green"},
            ],
        )

    def test_rejects_visible_block_page_marker(self) -> None:
        html = "<html><body>访问被拒绝，请稍后重试</body></html>"

        with self.assertRaisesRegex(RuntimeError, "拦截页"):
            extract_page_rows_from_html(html, "#odds2 table")


if __name__ == "__main__":
    unittest.main()
