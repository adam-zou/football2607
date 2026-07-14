import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

from fetch_data.status_cli import build_parser


class StatusCliTests(unittest.TestCase):
    def test_default_refresh_intervals_are_60_seconds(self) -> None:
        args = build_parser().parse_args([])

        self.assertEqual(args.list_refresh_seconds, 60.0)
        self.assertEqual(args.detail_refresh_seconds, 60.0)
        self.assertEqual(args.detail_batch_size, 10)
        self.assertEqual(args.odds_refresh_seconds, 5.0)
        self.assertEqual(args.odds_batch_size, 1)
        self.assertEqual(args.health_host, "127.0.0.1")
        self.assertEqual(args.health_port, 8080)

    def test_database_url_can_be_loaded_from_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DATABASE_URL=postgresql://test:test@localhost/test\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                load_dotenv(env_file)
                args = build_parser().parse_args([])

        self.assertEqual(
            args.database_url,
            "postgresql://test:test@localhost/test",
        )


if __name__ == "__main__":
    unittest.main()
