import unittest
from datetime import date
from urllib.error import HTTPError
from unittest.mock import patch

from fetch_archive_match_ids import (
    collect_match_ids,
    env_number,
    extract_match_ids_from_html,
    fetch_archive_match_ids,
    iter_dates_descending,
    parse_args,
    write_in_batches,
)


class FakeResponse:
    def __init__(self, source: bytes) -> None:
        self.source = source

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.source


class FakeUrlOpen:
    def __init__(self, response) -> None:
        self.response = response
        self.request = None
        self.timeout = None

    def __call__(self, request, timeout):
        self.request = request
        self.timeout = timeout
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ArchiveMatchIdTests(unittest.TestCase):
    def test_defaults_to_current_year_through_today(self) -> None:
        args = parse_args([], today=date(2026, 7, 24))

        self.assertEqual(args.start_date, date(2026, 1, 1))
        self.assertEqual(args.end_date, date(2026, 7, 24))

    def test_accepts_start_and_end_dates(self) -> None:
        args = parse_args(
            ["--start-date", "20260701", "--end-date", "20260703"],
            today=date(2026, 7, 24),
        )

        self.assertEqual(args.start_date, date(2026, 7, 1))
        self.assertEqual(args.end_date, date(2026, 7, 3))

    def test_rejects_reversed_date_range(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                ["--start-date", "20260703", "--end-date", "20260701"],
                today=date(2026, 7, 24),
            )

    def test_iterates_dates_from_latest_to_earliest(self) -> None:
        self.assertEqual(
            list(
                iter_dates_descending(
                    date(2026, 7, 1),
                    date(2026, 7, 3),
                )
            ),
            [date(2026, 7, 3), date(2026, 7, 2), date(2026, 7, 1)],
        )

    def test_extracts_unique_ids_and_allows_an_empty_table(self) -> None:
        source = b"""
            <table id="table_live">
              <tr sId="3006702"></tr>
              <tr sId="3013636"></tr>
              <tr sId="3006702"></tr>
            </table>
        """

        self.assertEqual(
            extract_match_ids_from_html(source),
            [3006702, 3013636],
        )
        self.assertEqual(
            extract_match_ids_from_html(b'<table id="table_live"></table>'),
            [],
        )

    def test_rejects_page_without_match_table(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "table_live"):
            extract_match_ids_from_html(b"<html></html>")

    def test_builds_url_from_date_without_a_browser(self) -> None:
        source = b'<table id="table_live"><tr sId="3006702"></tr></table>'
        open_url = FakeUrlOpen(FakeResponse(source))

        result = fetch_archive_match_ids(
            date(2026, 7, 1),
            12.5,
            open_url,
        )

        self.assertEqual(result, [3006702])
        self.assertEqual(open_url.timeout, 12.5)
        self.assertEqual(
            open_url.request.full_url,
            "https://bf.titan007.com/football/Over_20260701.htm",
        )

    def test_reports_http_error_with_date(self) -> None:
        error = HTTPError(
            "https://example.test/archive",
            503,
            "unavailable",
            {},
            None,
        )

        with self.assertRaisesRegex(RuntimeError, "20260701 返回 HTTP 503"):
            fetch_archive_match_ids(
                date(2026, 7, 1),
                15,
                FakeUrlOpen(error),
            )

    def test_collects_dates_in_reverse_order_and_deduplicates(self) -> None:
        calls = []

        def fetcher(archive_date):
            calls.append(archive_date)
            return {
                date(2026, 7, 3): [3, 2],
                date(2026, 7, 2): [2, 1],
                date(2026, 7, 1): [1, 0],
            }[archive_date]

        result = collect_match_ids(
            date(2026, 7, 1),
            date(2026, 7, 3),
            fetcher,
        )

        self.assertEqual(calls, [date(2026, 7, 3), date(2026, 7, 2), date(2026, 7, 1)])
        self.assertEqual(result, [3, 2, 1, 0])

    def test_writes_twenty_ids_per_round_and_sleeps_between_rounds(self) -> None:
        batches = []
        sleeps = []

        def writer(database_url, match_ids):
            batches.append((database_url, list(match_ids)))
            return len(match_ids)

        inserted = write_in_batches(
            "postgresql://test",
            list(range(45)),
            20,
            300,
            writer,
            sleeps.append,
        )

        self.assertEqual([len(batch) for _, batch in batches], [20, 20, 5])
        self.assertEqual(sleeps, [300, 300])
        self.assertEqual(inserted, 45)

    def test_reads_positive_numbers_from_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {"TEST_NUMBER": "20", "TEST_FLOAT": "300.5"},
        ):
            self.assertEqual(env_number("TEST_NUMBER", 1, integer=True), 20)
            self.assertEqual(env_number("TEST_FLOAT", 1), 300.5)


if __name__ == "__main__":
    unittest.main()
