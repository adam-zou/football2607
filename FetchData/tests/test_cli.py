"""Tests for the one-shot match-list command interface."""

import unittest

from fetch_data.cli import build_parser


class MatchCliTests(unittest.TestCase):
    def test_interface_only_exposes_source_and_headed_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args([])

        self.assertEqual(args.source, "titan007")
        self.assertFalse(args.headed)
        self.assertFalse(hasattr(args, "format"))
        self.assertFalse(hasattr(args, "timeout"))


if __name__ == "__main__":
    unittest.main()
