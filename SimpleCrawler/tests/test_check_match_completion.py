import argparse
import os
import sys
import unittest
from unittest import mock

from check_match_completion import (
    DETAIL_SCRIPT,
    mark_completed,
    parse_args,
    refresh_detail_once,
    run_final_jobs,
    select_finalization_matches,
)
from odds_collection import MarketCollectionOutcome, OddsPageJob


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


class FakeConnection:
    def __init__(self, rows=()) -> None:
        self.cursor_instance = FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


class FinalizationSelectionTests(unittest.TestCase):
    def test_selects_stable_finished_or_four_hours_old_matches(self) -> None:
        connection = FakeConnection([(1001, True, "未完成"), (1002, False, "异常")])

        matches = select_finalization_matches(connection, 25, ["未完成"])

        self.assertEqual(
            matches,
            [(1001, True, "未完成"), (1002, False, "异常")],
        )
        statement = connection.cursor_instance.statement
        self.assertIn("NOW() - INTERVAL '4 hours'", statement)
        self.assertIn("details.updated_at <= NOW() - INTERVAL '5 minutes'", statement)
        self.assertIn("暂停爬取", statement)
        self.assertIn("异常", statement)
        self.assertIn("details.status_text = '完'", statement)
        self.assertIn("OR", statement)
        self.assertIn("DESC", statement)
        self.assertEqual(
            connection.cursor_instance.parameters,
            (["未完成"], 25),
        )

    @mock.patch("check_match_completion.subprocess.run")
    def test_overdue_nonfinished_match_forces_one_detail_refresh(self, run) -> None:
        run.return_value.returncode = 0

        returncode = refresh_detail_once(123456)

        self.assertEqual(returncode, 0)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            command,
            [sys.executable, str(DETAIL_SCRIPT), "123456"],
        )
        self.assertEqual(
            environment["SIMPLE_CRAWLER_ACTIVE_CRAWL_STATUSES"],
            "未完成,暂停爬取,异常",
        )

    def test_mark_completed_is_the_only_terminal_update(self) -> None:
        connection = FakeConnection()

        mark_completed(connection, 123456)

        self.assertIn("crawl_status = '已完成'", connection.cursor_instance.statement)
        self.assertEqual(connection.cursor_instance.parameters, (123456,))
        self.assertEqual(connection.commits, 1)


class FinalizationArgumentTests(unittest.TestCase):
    def test_defaults_to_shared_page_concurrency(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            args = parse_args([])

        self.assertEqual(args.concurrency, 12)
        self.assertEqual(args.match_timeout, 180.0)
        self.assertEqual(args.company_ids, [3, 4, 8, 24, 31, 47])


class FinalPageRetryTests(unittest.IsolatedAsyncioTestCase):
    @mock.patch(
        "check_match_completion.persist_final_failure_or_log",
    )
    @mock.patch(
        "check_match_completion.persist_market_page",
    )
    @mock.patch(
        "check_match_completion.collect_company_markets_async",
        new_callable=mock.AsyncMock,
    )
    async def test_only_failed_market_is_retried(
        self,
        collect,
        persist_market_page,
        persist_failure,
    ) -> None:
        jobs = [
            OddsPageJob(123, 3, "handicap"),
            OddsPageJob(123, 3, "one_x_two"),
            OddsPageJob(123, 3, "over_under"),
        ]
        collect.side_effect = [
            [
                MarketCollectionOutcome(jobs[0], changes=[]),
                MarketCollectionOutcome(
                    jobs[1],
                    error=RuntimeError("one market failed"),
                ),
                MarketCollectionOutcome(jobs[2], changes=[]),
            ],
            [MarketCollectionOutcome(jobs[1], changes=[])],
        ]
        args = argparse.Namespace(
            base_url="https://example.test/{endpoint}",
            timeout=12.0,
            concurrency=4,
        )

        succeeded, failed = await run_final_jobs(
            mock.sentinel.browser,
            mock.sentinel.proxy_client,
            FakeConnection(),
            args,
            jobs,
        )

        self.assertEqual((succeeded, failed), (3, 0))
        self.assertEqual(collect.await_count, 2)
        retry_job = collect.await_args_list[1].args[3]
        self.assertEqual(retry_job.markets, ("one_x_two",))
        self.assertEqual(persist_market_page.call_count, 3)
        persist_failure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
