import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from auth import hash_password, load_users, save_users, verify_password


class MatchWebAppTests(unittest.TestCase):
    def setUp(self):
        self.app = server.MatchWebApp(
            "postgresql://test", {"adam": hash_password("secret-123")}, b"key"
        )

    def test_authenticate_requires_both_values(self):
        self.assertTrue(self.app.authenticate("adam", "secret-123"))
        self.assertFalse(self.app.authenticate("adam", "wrong"))
        self.assertFalse(self.app.authenticate("wrong", "secret-123"))

    def test_session_round_trip_and_tamper_detection(self):
        token = self.app.create_session("adam")
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

    def test_rejects_empty_status_selection_before_database_connection(self):
        with patch.object(server.psycopg2, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "状态无效"):
                server.fetch_matches("postgresql://test", "2026-07-17", [])
            connect.assert_not_called()

    def test_fetch_matches_uses_read_only_connection(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                3020831,
                "中超",
                "2026-07-17 19:35",
                "进行中",
                "主队",
                1,
                0,
                "客队",
                [{"company_id": 3, "change_time": "7-17 18:20"}],
            )
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
        self.assertIn("handicap.home_odds < 0.700", query)
        self.assertIn("handicap.away_odds < 0.700", query)
        self.assertIn("handicap.source_status <> '滚'", query)
        self.assertIn("totals.over_odds < 0.700", query)
        self.assertIn("totals.source_status <> '滚'", query)
        self.assertIn("filter_hits.markers IS NOT NULL", query)
        self.assertIn("company_three_handicap.company_id = 3", query)
        self.assertIn("company_three_one_x_two.company_id = 3", query)
        self.assertIn("company_three_totals.company_id = 3", query)
        self.assertEqual(matches[0]["match_id"], 3020831)
        self.assertEqual(matches[0]["home_score"], 1)
        self.assertEqual(
            matches[0]["filter_markers"],
            [
                {
                    "company_id": 3,
                    "company_name": "Crow*",
                    "change_time": "7-17 18:20",
                }
            ],
        )

    def test_fetch_matches_combines_multiple_status_groups(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(server.psycopg2, "connect", return_value=connection_context):
            server.fetch_matches(
                "postgresql://test", "2026-07-17", ["未开始", "进行中"]
            )

        query = cursor.execute.call_args.args[0]
        self.assertIn("details.status_text = '未开始'", query)
        self.assertIn("details.status_text IN ('上', '中', '下'", query)
        self.assertIn(" OR ", query)

    def test_fetch_matches_can_disable_odds_filter(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(server.psycopg2, "connect", return_value=connection_context):
            server.fetch_matches(
                "postgresql://test", "2026-07-17", "进行中", odds_filter=False
            )

        query = cursor.execute.call_args.args[0]
        self.assertIn("titan007_handicap_changes", query)
        self.assertIn("titan007_over_under_changes", query)
        self.assertNotIn("filter_hits.markers IS NOT NULL", query)
        self.assertNotIn("company_id = 3", query)

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

            body = "username=adam&password=secret-123"
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

    def test_user_management_requires_admin_and_persists_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.json"
            users = {
                "admin": hash_password("admin-secret"),
                "viewer": hash_password("viewer-secret"),
            }
            save_users(users_path, users)
            app = server.MatchWebApp(
                "postgresql://test", users, b"key", users_path
            )
            http_server = server.MatchWebServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            connection = HTTPConnection(*http_server.server_address, timeout=2)
            try:
                viewer_cookie = self._login_cookie(
                    connection, "viewer", "viewer-secret"
                )
                connection.request(
                    "GET", "/api/users", headers={"Cookie": viewer_cookie}
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)

                connection.request(
                    "GET", "/users", headers={"Cookie": viewer_cookie}
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)

                admin_cookie = self._login_cookie(
                    connection, "admin", "admin-secret"
                )
                connection.request(
                    "GET", "/users", headers={"Cookie": admin_cookie}
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)

                body = json.dumps({"username": "operator", "password": "new-secret"})
                connection.request(
                    "POST",
                    "/api/users",
                    body=body,
                    headers={
                        "Cookie": admin_cookie,
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 201)
                self.assertTrue(verify_password("new-secret", load_users(users_path)["operator"]))

                reset_body = json.dumps({"password": "reset-secret"})
                connection.request(
                    "PUT",
                    "/api/users/operator",
                    body=reset_body,
                    headers={
                        "Cookie": admin_cookie,
                        "Content-Type": "application/json",
                        "Content-Length": str(len(reset_body)),
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                self.assertTrue(
                    verify_password("reset-secret", load_users(users_path)["operator"])
                )

                connection.request(
                    "DELETE", "/api/users/admin", headers={"Cookie": admin_cookie}
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 400)
                self.assertIn("不能删除", payload["error"])

                connection.request(
                    "DELETE", "/api/users/operator", headers={"Cookie": admin_cookie}
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                self.assertNotIn("operator", load_users(users_path))
            finally:
                connection.close()
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=2)

    @staticmethod
    def _login_cookie(connection, username, password):
        body = f"username={username}&password={password}"
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
        if response.status != 303:
            raise AssertionError(f"login failed with status {response.status}")
        return response.getheader("Set-Cookie").split(";", 1)[0]

    def test_client_uses_required_odds_link(self):
        script = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("https://live.nowscore.com/odds/3in1Odds.aspx?companyid=3&id=", script)
        self.assertIn("60_000", script)
        self.assertIn("odds_filter", script)
        self.assertIn("filter_markers", script)

    def test_password_hash_is_salted_and_verifiable(self):
        first = hash_password("a-secure-password")
        second = hash_password("a-secure-password")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("a-secure-password", first))
        self.assertFalse(verify_password("wrong-password", first))

    def test_user_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.json"
            users = {"adam": hash_password("a-secure-password")}
            save_users(path, users)
            self.assertEqual(load_users(path), users)


if __name__ == "__main__":
    unittest.main()
