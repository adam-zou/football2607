import unittest
from unittest import mock

from fetch_odds_pages import (
    EXIT_INDETERMINATE,
    EXIT_MAJORITY_FAILURE,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    parse_args,
    result_exit_code,
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
    def test_defaults_to_four_workers(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            args = parse_args([])

        self.assertEqual(args.concurrency, 4)

    def test_environment_can_override_default(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"SIMPLE_CRAWLER_ODDS_PAGE_CONCURRENCY": "6"},
            clear=True,
        ):
            args = parse_args([])

        self.assertEqual(args.concurrency, 6)


if __name__ == "__main__":
    unittest.main()
