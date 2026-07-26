import unittest
from datetime import date
from urllib.error import HTTPError
from unittest.mock import patch

from fetch_archive_match_ids import (
    backfill_date_range,
    env_number,
    extract_match_ids_from_html,
    fetch_archive_match_ids,
    iter_dates_descending,
    parse_args,
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

    def test_ignores_empty_sid_rows_and_keeps_valid_matches(self) -> None:
        source = b"""
            <table id="table_live">
              <tr sId="3006702"></tr>
              <tr sId=""></tr>
              <tr sId="3013636"></tr>
            </table>
        """

        self.assertEqual(
            extract_match_ids_from_html(source),
            [3006702, 3013636],
        )

    def test_rejects_nonempty_invalid_sid(self) -> None:
        source = b'<table id="table_live"><tr sId="bad"></tr></table>'

        with self.assertRaisesRegex(RuntimeError, "无效 sId"):
            extract_match_ids_from_html(source)

    def test_rejects_page_without_match_table(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "table_live"):
            extract_match_ids_from_html(b"<html></html>")

    def test_extracts_ids_when_malformed_html_hides_table_from_dom_parser(self) -> None:
        source = b"""
            <html><body><style>
            <table id="table_live">
              <tr sId="3006702"></tr>
              <tr sId="3013636"></tr>
            </table>
            </body></html>
        """

        self.assertEqual(
            extract_match_ids_from_html(source),
            [3006702, 3013636],
        )

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

    def test_finishes_each_day_and_does_not_sleep_between_dates(self) -> None:
        events = []
        database_ids = {2}

        def fetcher(archive_date):
            events.append(("fetch", archive_date))
            return {
                date(2026, 7, 2): [4, 3, 2],
                date(2026, 7, 1): [2, 1],
            }[archive_date]

        def existing_loader(database_url, match_ids):
            events.append(("existing", list(match_ids)))
            return database_ids.intersection(match_ids)

        def writer(database_url, match_ids):
            events.append(("write", list(match_ids)))
            database_ids.update(match_ids)
            return len(match_ids)

        def sleeper(seconds):
            events.append(("sleep", seconds))

        inserted = backfill_date_range(
            "postgresql://test",
            date(2026, 7, 1),
            date(2026, 7, 2),
            20,
            300,
            fetcher,
            existing_loader,
            writer,
            sleeper,
        )

        self.assertEqual(inserted, 3)
        self.assertEqual(
            events,
            [
                ("fetch", date(2026, 7, 2)),
                ("existing", [4, 3, 2]),
                ("write", [4, 3]),
                ("fetch", date(2026, 7, 1)),
                ("existing", [2, 1]),
                ("write", [1]),
            ],
        )

    def test_writes_twenty_ids_per_round_and_sleeps_between_rounds(self) -> None:
        batches = []
        sleeps = []

        def writer(database_url, match_ids):
            batches.append(list(match_ids))
            return len(match_ids)

        inserted = backfill_date_range(
            "postgresql://test",
            date(2026, 7, 1),
            date(2026, 7, 1),
            20,
            300,
            lambda archive_date: list(range(45)),
            lambda database_url, match_ids: set(),
            writer,
            sleeps.append,
        )

        self.assertEqual([len(batch) for batch in batches], [20, 20, 5])
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
