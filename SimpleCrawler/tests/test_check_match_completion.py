import argparse
import asyncio
import os
import sys
import unittest
from unittest import mock

from check_match_completion import (
    DETAIL_SCRIPT,
    ENSURE_MATCH_STATUS_SQL,
    crawl_status_for_final_successes,
    finalize_match,
    finalize_matches,
    mark_crawl_status,
    parse_args,
    refresh_details_once,
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
        connection = FakeConnection([(1001, True, "未完成")])

        matches = select_finalization_matches(connection, 25)

        self.assertEqual(
            matches,
            [(1001, True, "未完成")],
        )
        statement = connection.cursor_instance.statement
        self.assertIn("NOW() - INTERVAL '4 hours'", statement)
        self.assertIn("details.updated_at <= NOW() - INTERVAL '5 minutes'", statement)
        self.assertIn(
            "details.status_text NOT IN ('推迟', '取消', '待定')",
            statement,
        )
        self.assertIn("details.updated_at <= NOW() - INTERVAL '7 days'", statement)
        self.assertIn("ids.crawl_status = '未完成'", statement)
        self.assertNotIn("暂停爬取", statement)
        self.assertNotIn("异常", statement)
        self.assertIn("details.status_text = '完'", statement)
        self.assertIn("OR", statement)
        self.assertIn("DESC", statement)
        self.assertEqual(
            connection.cursor_instance.parameters,
            (25,),
        )

    @mock.patch("check_match_completion.subprocess.run")
    def test_overdue_nonfinished_matches_share_one_detail_refresh(self, run) -> None:
        run.return_value.returncode = 0

        returncode = refresh_details_once([123456, 123457])

        self.assertEqual(returncode, 0)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            command,
            [
                sys.executable,
                "-u",
                str(DETAIL_SCRIPT),
                "123456",
                "123457",
            ],
        )
        self.assertEqual(
            environment["SIMPLE_CRAWLER_ACTIVE_CRAWL_STATUSES"],
            "未完成",
        )

    def test_updates_the_classified_crawl_status(self) -> None:
        connection = FakeConnection()

        mark_crawl_status(connection, 123456, "暂停爬取")

        self.assertIn("SET crawl_status = %s", connection.cursor_instance.statement)
        self.assertEqual(
            connection.cursor_instance.parameters,
            ("暂停爬取", 123456),
        )
        self.assertEqual(connection.commits, 1)

    def test_classifies_total_final_success_pages(self) -> None:
        expected = 18

        for successful_pages in range(0, 4):
            self.assertEqual(
                crawl_status_for_final_successes(successful_pages, expected),
                "未完成",
            )
        for successful_pages in range(4, 7):
            self.assertEqual(
                crawl_status_for_final_successes(successful_pages, expected),
                "异常",
            )
        for successful_pages in range(7, 18):
            self.assertEqual(
                crawl_status_for_final_successes(successful_pages, expected),
                "暂停爬取",
            )
        self.assertEqual(
            crawl_status_for_final_successes(18, expected),
            "已完成",
        )

    def test_status_constraint_is_rebuilt_only_when_definition_is_stale(self) -> None:
        self.assertIn("DO $$", ENSURE_MATCH_STATUS_SQL)
        self.assertIn("pg_get_constraintdef", ENSURE_MATCH_STATUS_SQL)
        self.assertIn("IF NOT EXISTS", ENSURE_MATCH_STATUS_SQL)


class FinalizationArgumentTests(unittest.TestCase):
    def test_defaults_to_shared_page_concurrency(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            args = parse_args([])

        self.assertEqual(args.concurrency, 12)
        self.assertEqual(args.match_concurrency, 2)
        self.assertEqual(args.match_timeout, 180.0)
        self.assertEqual(args.company_ids, [3, 4, 8, 24, 31, 47])


class FinalPageRetryTests(unittest.IsolatedAsyncioTestCase):
    @mock.patch("check_match_completion.mark_crawl_status")
    @mock.patch(
        "check_match_completion.load_pending_jobs",
        return_value=[],
    )
    @mock.patch(
        "check_match_completion.load_final_snapshot_success_count",
        return_value=7,
    )
    @mock.patch(
        "check_match_completion.prepare_pending_jobs",
        return_value=[],
    )
    async def test_final_match_uses_cumulative_successes_for_status(
        self,
        prepare_pending,
        load_success_count,
        load_pending,
        mark_status,
    ) -> None:
        args = argparse.Namespace(
            company_ids=[3, 4, 8, 24, 31, 47],
            match_timeout=180.0,
        )

        result = await finalize_match(
            mock.sentinel.browser,
            mock.sentinel.proxy_client,
            FakeConnection(),
            args,
            123,
        )

        self.assertEqual(result.successful_pages, 7)
        self.assertEqual(result.crawl_status, "暂停爬取")
        self.assertFalse(result.completed)
        mark_status.assert_called_once_with(
            mock.ANY,
            123,
            "暂停爬取",
        )

    @mock.patch(
        "check_match_completion.persist_market_batch",
    )
    @mock.patch(
        "check_match_completion.collect_company_markets_async",
        new_callable=mock.AsyncMock,
    )
    async def test_only_failed_market_is_retried(
        self,
        collect,
        persist_batch,
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
        self.assertEqual(persist_batch.call_count, 2)
        first_batch = persist_batch.call_args_list[0].args[1]
        self.assertEqual(len(first_batch), 3)


class FinalizationSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_matches_can_finalize_concurrently(self) -> None:
        active = 0
        maximum_active = 0

        async def fake_finalize(
            browser,
            proxy_client,
            connection,
            args,
            match_id,
            concurrency_limiter=None,
        ):
            nonlocal active, maximum_active
            self.assertIsNotNone(concurrency_limiter)
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1
            return mock.Mock(
                match_id=match_id,
                completed=True,
                crawl_status="已完成",
                successful_pages=18,
                timed_out=False,
                succeeded=18,
                failed=0,
                pending=0,
            )

        browser = mock.AsyncMock()
        playwright = mock.Mock()
        playwright.chromium.launch = mock.AsyncMock(return_value=browser)
        playwright_context = mock.AsyncMock()
        playwright_context.__aenter__.return_value = playwright
        args = argparse.Namespace(
            headed=False,
            company_ids=[3, 4, 8, 24, 31, 47],
            concurrency=6,
            match_concurrency=2,
        )

        with mock.patch(
            "check_match_completion.ProxyClient.from_env",
            return_value=mock.sentinel.proxy_client,
        ), mock.patch(
            "check_match_completion.async_playwright",
            return_value=playwright_context,
        ), mock.patch(
            "check_match_completion.finalize_match",
            side_effect=fake_finalize,
        ):
            results = await finalize_matches(
                FakeConnection(),
                args,
                [(1, True, "未完成"), (2, True, "未完成")],
            )

        self.assertEqual(maximum_active, 2)
        self.assertEqual([result.match_id for result in results], [1, 2])


if __name__ == "__main__":
    unittest.main()
