import unittest
from pathlib import Path
from unittest.mock import patch

from fetch_data.models import (
    HandicapChange,
    Movement,
    OddsMarketRequest,
    OddsMarketResult,
    OddsSnapshot,
    OneXTwoChange,
    OverUnderChange,
)
from fetch_data.odds_postgres import (
    BEGIN_ODDS_ATTEMPT,
    COUNT_PENDING_MATCH_IDS,
    FETCH_DUE_MARKETS,
    FETCH_PENDING_MATCH_IDS,
    INITIALIZE_ODDS_TABLES,
    PostgresOddsStore,
    RECORD_ODDS_FAILURE,
    RECORD_ODDS_SUCCESS,
    UPSERT_HANDICAP_FETCH_STATUS,
    UPSERT_ONE_X_TWO_FETCH_STATUS,
    UPSERT_OVER_UNDER_FETCH_STATUS,
)


def build_complete_snapshot() -> OddsSnapshot:
    common = {
        "match_id": 3020831,
        "company_id": 3,
        "seq": 1,
        "match_minute": None,
        "home_score": None,
        "away_score": None,
        "change_time": "7-13 11:15",
        "source_status": "早",
        "is_suspended": False,
    }
    return OddsSnapshot(
        match_id=3020831,
        companies={3: "Crow*"},
        handicap_changes=[
            HandicapChange(
                **common,
                home_odds=0.90,
                home_odds_movement=Movement.UP,
                handicap_raw="半球",
                handicap_value=0.5,
                handicap_movement=Movement.UNCHANGED,
                away_odds=0.90,
                away_odds_movement=Movement.DOWN,
            )
        ],
        one_x_two_changes=[
            OneXTwoChange(
                **common,
                home_win_odds=2.10,
                home_win_odds_movement=Movement.UNCHANGED,
                draw_odds=2.80,
                draw_odds_movement=Movement.UP,
                away_win_odds=3.50,
                away_win_odds_movement=Movement.DOWN,
            )
        ],
        over_under_changes=[
            OverUnderChange(
                **common,
                over_odds=0.90,
                over_odds_movement=Movement.UP,
                total_line_raw="2/2.5",
                total_line_value=2.25,
                total_line_movement=Movement.UNCHANGED,
                under_odds=0.90,
                under_odds_movement=Movement.DOWN,
            )
        ],
    )


class FakeCursor:
    def __init__(self) -> None:
        self.executions = []
        self.fetchone_results = []
        self.fetchall_results = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))

    def fetchone(self):
        if self.fetchone_results:
            return self.fetchone_results.pop(0)
        return ("match_status", "match_basic_info")

    def fetchall(self):
        return self.fetchall_results


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.cursor_instance


class PostgresOddsStoreTests(unittest.TestCase):
    def test_final_verification_happens_before_market_upserts(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        events = []

        def verify(cursor, snapshot):
            events.append("verify")
            return {}

        def write(*args, **kwargs):
            events.append("write")

        with patch.object(store, "_fetch_status_values", side_effect=verify):
            with patch(
                "fetch_data.odds_postgres.execute_values",
                side_effect=write,
            ):
                store._upsert_snapshot_sync(build_complete_snapshot())

        self.assertEqual(events[0], "verify")

    def test_pending_queue_filters_and_prioritizes_by_match_phase(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        store._connection.cursor_instance.fetchall_results = [(101,), (205,)]

        match_ids = store._fetch_pending_match_ids_sync(2)

        self.assertEqual(match_ids, [101, 205])
        statement, parameters = store._connection.cursor_instance.executions[0]
        self.assertEqual(statement, FETCH_PENDING_MATCH_IDS)
        self.assertEqual(parameters, (2,))
        self.assertIn("INTERVAL '24 hours'", statement)
        self.assertIn("INTERVAL '5 minutes'", statement)
        self.assertIn("INTERVAL '3 hours'", statement)
        self.assertIn("schedule.next_attempt_at", statement)
        self.assertIn("WHEN basic.status_text = '完' THEN 1", statement)
        self.assertIn("verification_version = 1", statement)

    def test_pending_queue_count_supports_backlog_metrics(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        store._connection.cursor_instance.fetchone_results = [(12,)]

        count = store._count_pending_match_ids_sync()

        self.assertEqual(count, 12)
        self.assertEqual(
            store._connection.cursor_instance.executions[0][0],
            COUNT_PENDING_MATCH_IDS,
        )

    def test_begin_attempt_creates_a_five_minute_lease(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        store._connection.cursor_instance.fetchall_results = [
            (4, "over_under")
        ]

        with patch("fetch_data.odds_postgres.execute_values") as execute:
            requests = store._begin_match_attempt_sync(3020831)

        statement, parameters = store._connection.cursor_instance.executions[0]
        self.assertEqual(statement, FETCH_DUE_MARKETS)
        self.assertEqual(parameters, (3020831,))
        self.assertEqual(requests, [OddsMarketRequest(4, "over_under")])
        self.assertEqual(execute.call_args.args[1], BEGIN_ODDS_ATTEMPT)
        self.assertIn("INTERVAL '5 minutes'", execute.call_args.kwargs["template"])

    def test_market_success_resets_only_its_backoff_and_uses_phase_cadence(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        request = OddsMarketRequest(4, "over_under")
        snapshot = OddsSnapshot(
            match_id=3020831,
            companies={4: "立*"},
            handicap_changes=[],
            one_x_two_changes=[],
            over_under_changes=[],
            market_results=[OddsMarketResult(request, True)],
        )

        with patch("fetch_data.odds_postgres.execute_values") as execute:
            store._record_market_outcomes_sync(snapshot)

        statement = execute.call_args.args[1]
        values = execute.call_args.args[2]
        self.assertEqual(statement, RECORD_ODDS_SUCCESS)
        self.assertEqual(values, [(3020831, 4, "over_under")])
        self.assertIn("INTERVAL '1 minute'", statement)
        self.assertIn("INTERVAL '8 hours'", statement)
        self.assertIn("basic.scheduled_at - INTERVAL '5 minutes'", statement)
        self.assertIn("consecutive_failures = 0", statement)
        self.assertIn("is_abnormal = FALSE", statement)
        self.assertIn("abnormal_since = NULL", statement)

    def test_market_failure_uses_independent_bounded_backoff(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        request = OddsMarketRequest(4, "over_under")

        with patch("fetch_data.odds_postgres.execute_values") as execute:
            store._record_market_failures_sync(
                3020831,
                [request],
                "temporary failure",
            )

        statement = execute.call_args.args[1]
        parameters = execute.call_args.args[2]
        self.assertEqual(statement, RECORD_ODDS_FAILURE)
        self.assertEqual(
            parameters,
            [(3020831, 4, "over_under", "temporary failure")],
        )
        self.assertIn("consecutive_failures + 1 = 1", statement)
        for delay in ("1 minute", "2 minutes", "5 minutes"):
            self.assertIn(delay, statement)
        self.assertIn("INTERVAL '3 hours'", statement)
        self.assertIn("is_abnormal", statement)
        self.assertIn("consecutive_failures + 1 >= 4", statement)

    def test_migration_matches_runtime_schema(self) -> None:
        odds_migration = (
            Path(__file__).parents[1]
            / "fetch_data"
            / "migrations"
            / "003_titan007_odds_changes.sql"
        ).read_text(encoding="utf-8")
        schedule_migration = (
            Path(__file__).parents[1]
            / "fetch_data"
            / "migrations"
            / "004_odds_schedule.sql"
        ).read_text(encoding="utf-8")
        migration = odds_migration.strip() + "\n\n" + schedule_migration.strip()

        self.assertEqual(migration.strip(), INITIALIZE_ODDS_TABLES.strip())
        self.assertIn("change_time TEXT NOT NULL", migration)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS titan007_odds_market_schedule",
            migration,
        )
        self.assertIn("PRIMARY KEY (match_id, company_id, market)", migration)
        self.assertIn("next_attempt_at TIMESTAMPTZ NOT NULL", migration)
        self.assertIn("is_abnormal BOOLEAN NOT NULL DEFAULT FALSE", migration)
        self.assertIn("abnormal_since TIMESTAMPTZ", migration)
        self.assertEqual(
            migration.count("created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            9,
        )
        self.assertEqual(
            migration.count("updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            9,
        )
        self.assertEqual(
            migration.count("PRIMARY KEY (match_id, company_id, seq)"),
            3,
        )

    def test_snapshot_is_upserted_to_three_tables_in_one_transaction(self) -> None:
        common = {
            "match_id": 3020831,
            "company_id": 3,
            "seq": 1,
            "match_minute": None,
            "home_score": None,
            "away_score": None,
            "change_time": "7-13 11:15",
            "source_status": "早",
            "is_suspended": False,
        }
        snapshot = OddsSnapshot(
            match_id=3020831,
            companies={3: "Crow*"},
            handicap_changes=[
                HandicapChange(
                    **common,
                    home_odds=0.90,
                    home_odds_movement=Movement.UP,
                    handicap_raw="半球",
                    handicap_value=0.5,
                    handicap_movement=Movement.UNCHANGED,
                    away_odds=0.90,
                    away_odds_movement=Movement.DOWN,
                )
            ],
            one_x_two_changes=[
                OneXTwoChange(
                    **common,
                    home_win_odds=2.10,
                    home_win_odds_movement=Movement.UNCHANGED,
                    draw_odds=2.80,
                    draw_odds_movement=Movement.UP,
                    away_win_odds=3.50,
                    away_win_odds_movement=Movement.DOWN,
                )
            ],
            over_under_changes=[
                OverUnderChange(
                    **common,
                    over_odds=0.90,
                    over_odds_movement=Movement.UP,
                    total_line_raw="2/2.5",
                    total_line_value=2.25,
                    total_line_movement=Movement.UNCHANGED,
                    under_odds=0.90,
                    under_odds_movement=Movement.DOWN,
                )
            ],
        )
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        store._connection.cursor_instance.fetchone_results = [
            ("match_basic_info",),
            (True,),
            (True,),
            (True,),
            (True,),
            ("match_status", "match_basic_info"),
        ]

        with patch("fetch_data.odds_postgres.execute_values") as execute:
            store._upsert_snapshot_sync(snapshot)

        self.assertEqual(execute.call_count, 6)
        statements = [call.args[1] for call in execute.call_args_list]
        self.assertIn("titan007_handicap_changes", statements[0])
        self.assertIn("titan007_1x2_changes", statements[1])
        self.assertIn("titan007_over_under_changes", statements[2])
        self.assertEqual(
            statements[3:6],
            [
                UPSERT_HANDICAP_FETCH_STATUS,
                UPSERT_ONE_X_TWO_FETCH_STATUS,
                UPSERT_OVER_UNDER_FETCH_STATUS,
            ],
        )
        self.assertIn("updated_at = NOW()", statements[0])
        self.assertIn("updated_at = NOW()", statements[1])
        self.assertIn("updated_at = NOW()", statements[2])
        handicap_values = execute.call_args_list[0].args[2][0]
        self.assertEqual(handicap_values[:3], (3020831, 3, 1))
        self.assertEqual(handicap_values[10], "上升")
        self.assertEqual(handicap_values[13], "不变")
        self.assertEqual(handicap_values[15], "下降")
        self.assertEqual(
            [call.args[2][0] for call in execute.call_args_list[3:6]],
            [
                (3020831, 3, True, 1, 1),
                (3020831, 3, True, 1, 1),
                (3020831, 3, True, 1, 1),
            ],
        )
        completion_statements = store._connection.cursor_instance.executions
        self.assertIn("status_text = '完'", completion_statements[1][0])
        self.assertIn("IS NOT DISTINCT FROM", completion_statements[2][0])
        self.assertIn("crawl_status = '已完成'", completion_statements[-1][0])

    def test_partial_snapshot_writes_only_successful_market(self) -> None:
        complete = build_complete_snapshot()
        successful = OddsMarketRequest(3, "handicap")
        failed = OddsMarketRequest(3, "over_under")
        snapshot = OddsSnapshot(
            match_id=complete.match_id,
            companies=complete.companies,
            handicap_changes=complete.handicap_changes,
            one_x_two_changes=[],
            over_under_changes=[],
            market_results=[
                OddsMarketResult(successful, True),
                OddsMarketResult(failed, False, "temporary failure"),
            ],
        )
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        store._connection.cursor_instance.fetchone_results = [
            ("match_basic_info",),
            (False,),
            ("match_status", "match_basic_info"),
        ]

        with patch("fetch_data.odds_postgres.execute_values") as execute:
            store._upsert_snapshot_sync(snapshot)

        statements = [call.args[1] for call in execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn("titan007_handicap_changes", statements[0])
        self.assertEqual(statements[1], UPSERT_HANDICAP_FETCH_STATUS)
        self.assertNotIn("titan007_1x2_changes", "".join(statements))
        self.assertNotIn("titan007_over_under_changes", "".join(statements))

    def test_final_empty_markets_matching_empty_database_are_complete(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        store._connection = FakeConnection()
        store._connection.cursor_instance.fetchone_results = [
            ("match_basic_info",),
            (True,),
            (True,),
            (True,),
            (True,),
            ("match_status", "match_basic_info"),
        ]
        snapshot = OddsSnapshot(
            match_id=3020831,
            companies={47: "平*"},
            handicap_changes=[],
            one_x_two_changes=[],
            over_under_changes=[],
        )

        with patch("fetch_data.odds_postgres.execute_values") as execute:
            store._upsert_snapshot_sync(snapshot)

        self.assertEqual(
            [call.args[2][0] for call in execute.call_args_list],
            [
                (3020831, 47, True, None, 1),
                (3020831, 47, True, None, 1),
                (3020831, 47, True, None, 1),
            ],
        )

    def test_unfinished_match_does_not_verify_final_odds(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        cursor = FakeCursor()
        cursor.fetchone_results = [("match_basic_info",), (False,)]

        values = store._fetch_status_values(cursor, build_complete_snapshot())

        self.assertEqual(
            values,
            {
                "handicap": [(3020831, 3, False, None, 1)],
                "one_x_two": [(3020831, 3, False, None, 1)],
                "over_under": [(3020831, 3, False, None, 1)],
            },
        )
        self.assertFalse(
            any("IS NOT DISTINCT FROM" in statement for statement, _ in cursor.executions)
        )
        self.assertIn("INTERVAL '3 hours'", cursor.executions[1][0])

    def test_empty_page_with_existing_database_rows_is_not_complete(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        cursor = FakeCursor()
        cursor.fetchone_results = [
            ("match_basic_info",),
            (True,),
            (False,),
            (True,),
            (True,),
        ]
        snapshot = OddsSnapshot(
            match_id=3020831,
            companies={47: "平*"},
            handicap_changes=[],
            one_x_two_changes=[],
            over_under_changes=[],
        )

        values = store._fetch_status_values(cursor, snapshot)

        self.assertEqual(
            values,
            {
                "handicap": [(3020831, 47, False, None, 1)],
                "one_x_two": [(3020831, 47, True, None, 1)],
                "over_under": [(3020831, 47, True, None, 1)],
            },
        )

    def test_mismatched_latest_market_row_is_not_complete(self) -> None:
        store = PostgresOddsStore("postgresql://example/football")
        cursor = FakeCursor()
        cursor.fetchone_results = [
            ("match_basic_info",),
            (True,),
            (False,),
            (True,),
            (True,),
        ]

        values = store._fetch_status_values(cursor, build_complete_snapshot())

        self.assertEqual(
            values,
            {
                "handicap": [(3020831, 3, False, None, 1)],
                "one_x_two": [(3020831, 3, True, 1, 1)],
                "over_under": [(3020831, 3, True, 1, 1)],
            },
        )
