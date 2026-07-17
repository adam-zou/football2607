import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


class MatchWebAppTests(unittest.TestCase):
    def setUp(self):
        self.app = server.MatchWebApp("postgresql://test", "adam", "secret", b"key")

    def test_authenticate_requires_both_values(self):
        self.assertTrue(self.app.authenticate("adam", "secret"))
        self.assertFalse(self.app.authenticate("adam", "wrong"))
        self.assertFalse(self.app.authenticate("wrong", "secret"))

    def test_session_round_trip_and_tamper_detection(self):
        token = self.app.create_session()
        self.assertTrue(self.app.valid_session(token))
        self.assertFalse(self.app.valid_session(token + "x"))

    def test_validate_date(self):
        self.assertEqual(server.validate_date("2026-07-17"), "2026-07-17")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            server.validate_date("17/07/2026")

    def test_rejects_unknown_status_before_database_connection(self):
        with patch.object(server.psycopg2, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "状态无效"):
                server.fetch_matches("postgresql://test", "2026-07-17", "全部")
            connect.assert_not_called()

    def test_fetch_matches_uses_read_only_connection(self):
        updated_at = datetime.now(server.SHANGHAI) - timedelta(minutes=1)
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (3020831, "中超", "2026-07-17 19:35", "进行中", "主队", 1, 0, "客队", "未完成", updated_at)
        ]
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(server.psycopg2, "connect", return_value=connection_context):
            matches = server.fetch_matches(
                "postgresql://test", "2026-07-17", "进行中"
            )

        connection.set_session.assert_called_once_with(readonly=True)
        cursor.execute.assert_called_once()
        query = cursor.execute.call_args.args[0]
        self.assertIn("(''|′)", query)
        self.assertEqual(matches[0]["match_id"], 3020831)
        self.assertEqual(matches[0]["home_score"], 1)

    def test_http_login_protects_home_page(self):
        http_server = server.MatchWebServer(("127.0.0.1", 0), self.app)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection(*http_server.server_address, timeout=2)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 303)
            self.assertEqual(response.getheader("Location"), "/login")

            body = "username=adam&password=secret"
            connection.request(
                "POST",
                "/login",
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 303)
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]

            connection.request("GET", "/", headers={"Cookie": cookie})
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("每 60 秒自动刷新", page)
        finally:
            connection.close()
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=2)

    def test_client_uses_required_odds_link(self):
        script = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("changeDetail/3in1Odds.aspx?id=", script)
        self.assertIn("companyid=47&l=0", script)
        self.assertIn("60_000", script)


if __name__ == "__main__":
    unittest.main()
