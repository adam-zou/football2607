import unittest

from fetch_data.odds_cli import build_parser


class OddsCliTests(unittest.TestCase):
    def test_match_id_is_required_and_all_companies_are_default(self) -> None:
        args = build_parser().parse_args(["3020831"])

        self.assertEqual(args.match_id, 3020831)
        self.assertIsNone(args.company_ids)
        self.assertEqual(args.concurrency, 6)

    def test_company_filter_can_be_repeated(self) -> None:
        args = build_parser().parse_args(
            ["3020831", "--company-id", "3", "--company-id", "47"]
        )

        self.assertEqual(args.company_ids, [3, 47])


if __name__ == "__main__":
    unittest.main()
