import base64
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
from simple_crawler.monitoring import DashboardServer, RuntimeMonitor


class MatchWebAppTests(unittest.TestCase):
    def setUp(self):
        self.replace_session_patch = patch.object(
            server.MatchWebApp, "_replace_active_session"
        )
        self.match_session_patch = patch.object(
            server.MatchWebApp, "_active_session_matches", return_value=True
        )
        self.revoke_session_patch = patch.object(
            server.MatchWebApp, "revoke_active_session"
        )
        self.replace_session_patch.start()
        self.match_session_patch.start()
        self.revoke_session_patch.start()
        self.addCleanup(self.replace_session_patch.stop)
        self.addCleanup(self.match_session_patch.stop)
        self.addCleanup(self.revoke_session_patch.stop)
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

    def test_new_user_named_login_replaces_previous_session(self):
        active_sessions = {}
        server.MatchWebApp._replace_active_session.side_effect = (
            lambda username, session_id, _expires_at: active_sessions.__setitem__(
                username, session_id
            )
        )
        server.MatchWebApp._active_session_matches.side_effect = (
            lambda username, session_id: active_sessions.get(username) == session_id
        )
        app = server.MatchWebApp(
            "postgresql://test",
            {"matchuser": hash_password("user-secret")},
            b"key",
        )
        first = app.create_session("matchuser")
        second = app.create_session("matchuser")

        first_session_id = json.loads(
            base64.urlsafe_b64decode(first.split(".", 1)[0] + "==")
        )["session_id"]
        second_session_id = json.loads(
            base64.urlsafe_b64decode(second.split(".", 1)[0] + "==")
        )["session_id"]
        self.assertNotEqual(first_session_id, second_session_id)
        self.assertEqual(
            server.MatchWebApp._replace_active_session.call_count,
            2,
        )
        self.assertFalse(app.valid_session(first))
        self.assertTrue(app.valid_session(second))

    def test_non_user_named_accounts_do_not_use_single_session_registry(self):
        app = server.MatchWebApp(
            "postgresql://test",
            {
                "001": hash_password("account-secret"),
                "admin": hash_password("admin-secret"),
            },
            b"key",
        )
        first_001 = app.create_session("001")
        second_001 = app.create_session("001")
        admin_token = app.create_session("admin")

        self.assertTrue(app.valid_session(first_001))
        self.assertTrue(app.valid_session(second_001))
        self.assertTrue(app.valid_session(admin_token))
        server.MatchWebApp._replace_active_session.assert_not_called()

    def test_validate_date(self):
        self.assertEqual(server.validate_date("2026-07-17"), "2026-07-17")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            server.validate_date("17/07/2026")

    def test_pb_only_username_is_case_insensitive(self):
        self.assertTrue(server.is_pb_only_username("matchuser"))
        self.assertTrue(server.is_pb_only_username("MatchUser01"))
        self.assertFalse(server.is_pb_only_username("viewer"))

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
        self.assertIn(
            "details.scheduled_time::TIMESTAMP >= (%s::DATE - INTERVAL '3 hours')",
            query,
        )
        self.assertIn(
            "details.scheduled_time::TIMESTAMP < (%s::DATE + INTERVAL '1 day')",
            query,
        )
        self.assertEqual(cursor.execute.call_args.args[1], ("2026-07-17", "2026-07-17"))
        self.assertIn("(''|′)", query)
        self.assertIn("handicap.home_odds < 0.700", query)
        self.assertIn("handicap.away_odds < 0.700", query)
        self.assertIn("handicap.company_id <> 4", query)
        self.assertIn("handicap.source_status <> '滚'", query)
        self.assertIn("totals.over_odds < 0.700", query)
        self.assertIn("totals.company_id <> 4", query)
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

    def test_fetch_company_47_suspensions_uses_consecutive_live_runs(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                3020831,
                "中超",
                "2026-07-17 19:35",
                "完",
                "主队",
                2,
                1,
                "客队",
                "关注",
                [
                    {"change_time": "07-17 20:15", "match_minute": 42},
                    {"change_time": "07-17 20:18", "match_minute": None},
                ],
            )
        ]
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(server.psycopg2, "connect", return_value=connection_context):
            matches = server.fetch_company_47_suspensions(
                "postgresql://test", "2026-07-17"
            )

        connection.set_session.assert_called_once_with(readonly=True)
        query = cursor.execute.call_args.args[0]
        self.assertIn("titan007_1x2_changes", query)
        self.assertIn("changes.company_id = 47", query)
        self.assertIn("details.status_text IN ('上', '中', '下'", query)
        self.assertIn(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", query)
        self.assertIn(r"^\d{1,2}-\d{1,2} \d{1,2}:\d{2}$", query)
        self.assertIn("source_status = '滚'", query)
        self.assertIn("AND is_suspended", query)
        self.assertIn("seq - ROW_NUMBER()", query)
        self.assertIn("next_row.seq = suspension_runs.end_seq + 1", query)
        self.assertIn("INTERVAL '3 minutes'", query)
        self.assertEqual(cursor.execute.call_args.args[1], ("2026-07-17", "2026-07-17"))
        self.assertEqual(matches[0]["match_id"], 3020831)
        self.assertNotIn("suspension_periods", matches[0])
        self.assertEqual(matches[0]["pb_status"], "关注")
        self.assertEqual(
            matches[0]["suspension_points"],
            [
                {"change_time": "07-17 20:15", "match_minute": 42},
                {"change_time": "07-17 20:18", "match_minute": None},
            ],
        )

    def test_fetch_company_47_suspensions_combines_status_groups(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(server.psycopg2, "connect", return_value=connection_context):
            server.fetch_company_47_suspensions(
                "postgresql://test", "2026-07-17", ["未开始", "进行中"]
            )

        query = cursor.execute.call_args.args[0]
        self.assertIn("details.status_text = '未开始'", query)
        self.assertIn("details.status_text IN ('上', '中', '下'", query)
        self.assertIn("SELECT DISTINCT", query)

        with patch.object(server.psycopg2, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "状态无效"):
                server.fetch_company_47_suspensions(
                    "postgresql://test", "2026-07-17", ["完"]
                )
            connect.assert_not_called()

    def test_ensure_pb_status_table_creates_matchweb_owned_table(self):
        cursor = MagicMock()
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(server.psycopg2, "connect", return_value=connection_context):
            server.ensure_pb_status_table("postgresql://test")

        query = cursor.execute.call_args.args[0]
        self.assertIn("CREATE TABLE IF NOT EXISTS match_web_pb_status", query)
        self.assertIn("status IN ('关注', '作废')", query)

    def test_single_session_registry_hashes_and_checks_session_id(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection
        expires_at = server.datetime.now(server.SHANGHAI) + server.SESSION_LIFETIME

        with patch.object(server.psycopg2, "connect", return_value=connection_context):
            server.ensure_user_session_table("postgresql://test")
            create_query = cursor.execute.call_args.args[0]
            self.assertIn(
                "CREATE TABLE IF NOT EXISTS match_web_user_session", create_query
            )

            server.replace_active_session(
                "postgresql://test", "viewer", "raw-session-id", expires_at
            )
            replace_query, replace_params = cursor.execute.call_args.args
            self.assertIn("ON CONFLICT (username) DO UPDATE", replace_query)
            self.assertNotIn("raw-session-id", replace_params)
            self.assertEqual(len(replace_params[1]), 64)

            self.assertTrue(
                server.active_session_matches(
                    "postgresql://test", "viewer", "raw-session-id"
                )
            )
            connection.set_session.assert_called_once_with(readonly=True)

            server.delete_active_session("postgresql://test", "viewer")
            delete_query, delete_params = cursor.execute.call_args.args
            self.assertIn("DELETE FROM match_web_user_session", delete_query)
            self.assertEqual(delete_params, ("viewer",))

    def test_set_pb_match_status_upserts_shared_status(self):
        cursor = MagicMock()
        cursor.rowcount = 1
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(server.psycopg2, "connect", return_value=connection_context):
            server.set_pb_match_status(
                "postgresql://test", 3020831, "作废", "matchuser"
            )

        query, params = cursor.execute.call_args.args
        self.assertIn("ON CONFLICT (match_id) DO UPDATE", query)
        self.assertEqual(params, (3020831, "作废", "matchuser", 3020831))

        with self.assertRaisesRegex(ValueError, "状态无效"):
            server.set_pb_match_status(
                "postgresql://test", 3020831, "未知", "matchuser"
            )

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

            connection.request(
                "GET", "/company-47-suspensions", headers={"Cookie": cookie}
            )
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("符合条件的比赛", page)
            self.assertNotIn("公司 47 滚球封盘", page)
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

    def test_username_containing_user_can_only_access_pb_page(self):
        app = server.MatchWebApp(
            "postgresql://test",
            {"matchuser": hash_password("user-secret")},
            b"key",
        )
        http_server = server.MatchWebServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection(*http_server.server_address, timeout=2)
        app.set_pb_match_status = MagicMock()
        try:
            body = "username=matchuser&password=user-secret"
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
            self.assertEqual(response.getheader("Location"), server.PB_ONLY_PAGE)
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]

            connection.request(
                "GET", server.PB_ONLY_PAGE, headers={"Cookie": cookie}
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)

            connection.request("GET", "/", headers={"Cookie": cookie})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)

            connection.request(
                "GET", "/api/matches?date=2026-07-17", headers={"Cookie": cookie}
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 403)
            self.assertEqual(payload["error"], "该账号仅可访问 PB 页面")

            status_body = json.dumps({"status": "关注"}, ensure_ascii=False).encode("utf-8")
            connection.request(
                "PUT",
                "/api/company-47-suspensions/3020831/status",
                body=status_body,
                headers={
                    "Cookie": cookie,
                    "Content-Type": "application/json",
                    "Content-Length": str(len(status_body)),
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "关注")
            app.set_pb_match_status.assert_called_once_with(
                3020831, "关注", "matchuser"
            )
        finally:
            connection.close()
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=2)

    def test_monitor_route_is_admin_only_and_proxies_local_dashboard(self):
        monitor = RuntimeMonitor()
        monitor.append_log("fetch_match_ids", "监控代理测试")
        dashboard = DashboardServer(monitor, "127.0.0.1", 0)
        dashboard.start()
        host, port = dashboard.address
        users = {
            "admin": hash_password("admin-secret"),
            "viewer": hash_password("viewer-secret"),
        }
        app = server.MatchWebApp(
            "postgresql://test",
            users,
            b"key",
            monitor_url=f"http://{host}:{port}",
        )
        http_server = server.MatchWebServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection(*http_server.server_address, timeout=2)
        try:
            connection.request("GET", "/monitor/api/status")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 401)

            viewer_cookie = self._login_cookie(
                connection, "viewer", "viewer-secret"
            )
            connection.request(
                "GET", "/monitor/", headers={"Cookie": viewer_cookie}
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)

            admin_cookie = self._login_cookie(connection, "admin", "admin-secret")
            connection.request("GET", "/monitor", headers={"Cookie": admin_cookie})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 303)
            self.assertEqual(response.getheader("Location"), "/monitor/")

            connection.request("GET", "/monitor/", headers={"Cookie": admin_cookie})
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("SimpleCrawler 总监控", page)
            self.assertIn("fetch('api/status'", page)

            connection.request(
                "GET", "/monitor/api/status", headers={"Cookie": admin_cookie}
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertIn("监控代理测试", payload["components"][1]["logs"][0])

            dashboard.close()
            connection.request(
                "GET", "/monitor/api/status", headers={"Cookie": admin_cookie}
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 503)
            self.assertEqual(payload["error"], "采集监控暂不可用")
        finally:
            connection.close()
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=2)
            dashboard.close()

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
        self.assertIn("monitor-link", script)

        suspension_script = (
            server.STATIC_DIR / "company-47-suspensions.js"
        ).read_text(encoding="utf-8")
        self.assertIn("/api/company-47-suspensions", suspension_script)
        self.assertIn("companyid=47", suspension_script)
        self.assertNotIn("suspension_periods", suspension_script)
        self.assertIn("suspension_points", suspension_script)
        self.assertIn("比赛分钟", suspension_script)
        self.assertIn("赛前预警", suspension_script)
        self.assertIn("滚球预警", suspension_script)
        self.assertIn("60_000", suspension_script)

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
