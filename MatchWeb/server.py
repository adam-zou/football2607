#!/usr/bin/env python3
"""Authenticated read-only web view for crawled Titan007 matches."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv

from auth import hash_password, load_users, save_users, validate_username, verify_password


SIMPLE_CRAWLER_DIR = Path(__file__).resolve().parent.parent / "SimpleCrawler"
if str(SIMPLE_CRAWLER_DIR) not in sys.path:
    sys.path.insert(0, str(SIMPLE_CRAWLER_DIR))

from simple_crawler.companies import COMPANY_NAMES


APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSION_COOKIE = "match_web_session"
SESSION_LIFETIME = timedelta(hours=12)
ALLOWED_STATUSES = {"未开始", "进行中", "完", "其它"}
DEFAULT_STATUSES = ("未开始", "进行中")
PB_ALLOWED_STATUSES = set(DEFAULT_STATUSES)
ADMIN_USERNAME = "admin"
DEFAULT_MONITOR_URL = "http://127.0.0.1:8081"
MONITOR_TIMEOUT_SECONDS = 2
PB_ONLY_PAGE = "/company-47-suspensions"
PB_ONLY_PATHS = {
    PB_ONLY_PAGE,
    "/company-47-suspensions.js",
    "/api/company-47-suspensions",
    "/logout",
}
PB_MATCH_STATUSES = {"关注", "作废"}
PB_STATUS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS match_web_pb_status (
    match_id BIGINT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('关注', '作废')),
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""
USER_SESSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS match_web_user_session (
    username TEXT PRIMARY KEY,
    token_hash CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

STATUS_SQL = {
    "未开始": "details.status_text = '未开始'",
    "进行中": """(
        details.status_text IN ('上', '中', '下', '加', '点', '进行中')
        OR details.status_text ~ '^[0-9]+(\\+[0-9]+)?(''|′)$'
    )""",
    "完": "details.status_text = '完'",
    "其它": """(
        details.status_text NOT IN (
            '未开始', '上', '中', '下', '加', '点', '进行中', '完'
        )
        AND details.status_text !~ '^[0-9]+(\\+[0-9]+)?(''|′)$'
    )""",
}


def load_environment() -> None:
    """Load crawler connection settings, then optional web overrides."""

    load_dotenv(REPO_DIR / "SimpleCrawler" / ".env")
    load_dotenv(APP_DIR / ".env", override=True)


def today_in_shanghai() -> str:
    return datetime.now(SHANGHAI).date().isoformat()


def validate_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("日期格式必须是 YYYY-MM-DD") from exc


def is_pb_only_username(username: Optional[str]) -> bool:
    return bool(username and "user" in username.casefold())


def ensure_pb_status_table(database_url: str) -> None:
    """Create the MatchWeb-owned PB status table when it does not exist."""

    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(PB_STATUS_TABLE_SQL)


def ensure_user_session_table(database_url: str) -> None:
    """Create the MatchWeb-owned single-session registry when absent."""

    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(USER_SESSION_TABLE_SQL)


def replace_active_session(
    database_url: str,
    username: str,
    session_id: str,
    expires_at: datetime,
) -> None:
    token_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    query = """
        INSERT INTO match_web_user_session (username, token_hash, expires_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (username) DO UPDATE SET
            token_hash = EXCLUDED.token_hash,
            expires_at = EXCLUDED.expires_at,
            updated_at = NOW()
    """
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (username, token_hash, expires_at))


def active_session_matches(
    database_url: str,
    username: str,
    session_id: str,
) -> bool:
    token_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    query = """
        SELECT EXISTS (
            SELECT 1
            FROM match_web_user_session
            WHERE username = %s
              AND token_hash = %s
              AND expires_at > NOW()
        )
    """
    with psycopg2.connect(database_url) as connection:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute(query, (username, token_hash))
            return bool(cursor.fetchone()[0])


def delete_active_session(database_url: str, username: str) -> None:
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM match_web_user_session WHERE username = %s",
                (username,),
            )


def set_pb_match_status(
    database_url: str,
    match_id: int,
    status: str,
    updated_by: str,
) -> None:
    """Persist the shared PB status for one known match."""

    if match_id <= 0:
        raise ValueError("比赛 ID 无效")
    if status not in PB_MATCH_STATUSES:
        raise ValueError("PB 状态无效")
    query = """
        INSERT INTO match_web_pb_status (match_id, status, updated_by)
        SELECT %s, %s, %s
        WHERE EXISTS (
            SELECT 1 FROM match_details WHERE match_id = %s
        )
        ON CONFLICT (match_id) DO UPDATE SET
            status = EXCLUDED.status,
            updated_by = EXCLUDED.updated_by,
            updated_at = NOW()
    """
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (match_id, status, updated_by, match_id))
            if cursor.rowcount == 0:
                raise ValueError("比赛不存在")


def fetch_matches(
    database_url: str,
    match_date: str,
    statuses: Union[str, Sequence[str]],
    odds_filter: bool = True,
) -> List[Dict[str, object]]:
    """Return matching rows from the crawler database without changing it."""

    match_date = validate_date(match_date)
    if isinstance(statuses, str):
        statuses = [statuses]
    statuses = list(dict.fromkeys(statuses))
    if not statuses or any(status not in ALLOWED_STATUSES for status in statuses):
        raise ValueError("比赛状态无效")
    status_filter_sql = " OR ".join(f"({STATUS_SQL[status]})" for status in statuses)

    odds_filter_sql = ""
    if odds_filter:
        odds_filter_sql = """
          AND filter_hits.markers IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM titan007_handicap_changes AS company_three_handicap
              WHERE company_three_handicap.match_id = details.match_id
                AND company_three_handicap.company_id = 3
              UNION ALL
              SELECT 1
              FROM titan007_1x2_changes AS company_three_one_x_two
              WHERE company_three_one_x_two.match_id = details.match_id
                AND company_three_one_x_two.company_id = 3
              UNION ALL
              SELECT 1
              FROM titan007_over_under_changes AS company_three_totals
              WHERE company_three_totals.match_id = details.match_id
                AND company_three_totals.company_id = 3
          )
        """

    query = f"""
        SELECT
            details.match_id,
            details.league,
            details.scheduled_time,
            details.status_text,
            details.home_team,
            details.home_score,
            details.away_score,
            details.away_team,
            COALESCE(filter_hits.markers, '[]'::JSONB)
        FROM match_details AS details
        LEFT JOIN LATERAL (
            SELECT JSONB_AGG(
                JSONB_BUILD_OBJECT(
                    'company_id', matched.company_id,
                    'change_time', matched.change_time
                )
                ORDER BY matched.company_id, matched.change_time
            ) AS markers
            FROM (
                SELECT handicap.company_id, handicap.change_time
                FROM titan007_handicap_changes AS handicap
                WHERE handicap.match_id = details.match_id
                  AND handicap.company_id <> 4
                  AND handicap.source_status <> '滚'
                  AND (handicap.home_odds < 0.700 OR handicap.away_odds < 0.700)
                UNION
                SELECT totals.company_id, totals.change_time
                FROM titan007_over_under_changes AS totals
                WHERE totals.match_id = details.match_id
                  AND totals.company_id <> 4
                  AND totals.source_status <> '滚'
                  AND totals.over_odds < 0.700
            ) AS matched
        ) AS filter_hits ON TRUE
        WHERE details.scheduled_time ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}} \\d{{2}}:\\d{{2}}$'
          AND details.scheduled_time::TIMESTAMP >= (%s::DATE - INTERVAL '3 hours')
          AND details.scheduled_time::TIMESTAMP < (%s::DATE + INTERVAL '1 day')
          AND ({status_filter_sql})
          {odds_filter_sql}
        ORDER BY details.scheduled_time ASC, details.match_id ASC
    """

    with psycopg2.connect(database_url) as connection:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute(query, (match_date, match_date))
            rows = cursor.fetchall()

    matches = []
    for row in rows:
        markers = [
            {
                "company_id": int(marker["company_id"]),
                "company_name": COMPANY_NAMES.get(
                    int(marker["company_id"]), f"公司 {marker['company_id']}"
                ),
                "change_time": str(marker["change_time"]),
            }
            for marker in row[8]
        ]
        matches.append({
            "match_id": int(row[0]),
            "league": row[1],
            "scheduled_time": row[2],
            "status_text": row[3],
            "home_team": row[4],
            "home_score": row[5],
            "away_score": row[6],
            "away_team": row[7],
            "filter_markers": markers,
        })
    return matches


def fetch_company_47_suspensions(
    database_url: str,
    match_date: str,
    statuses: Union[str, Sequence[str]] = DEFAULT_STATUSES,
) -> List[Dict[str, object]]:
    """Return matches with a proven 3-minute company-47 live 1x2 suspension."""

    match_date = validate_date(match_date)
    if isinstance(statuses, str):
        statuses = [statuses]
    statuses = list(dict.fromkeys(statuses))
    if not statuses or any(status not in PB_ALLOWED_STATUSES for status in statuses):
        raise ValueError("比赛状态无效")
    status_filter_sql = " OR ".join(f"({STATUS_SQL[status]})" for status in statuses)

    query = f"""
        WITH company_rows_with_raw_time AS (
            SELECT
                details.match_id,
                details.league,
                details.scheduled_time,
                details.status_text,
                details.home_team,
                details.home_score,
                details.away_score,
                details.away_team,
                changes.seq,
                changes.match_minute,
                changes.change_time,
                changes.source_status,
                changes.is_suspended,
                details.scheduled_time::TIMESTAMP AS scheduled_at,
                TO_TIMESTAMP(
                    EXTRACT(YEAR FROM details.scheduled_time::TIMESTAMP)::INTEGER
                    || '-' || changes.change_time,
                    'YYYY-MM-DD HH24:MI'
                ) AS raw_change_at
            FROM match_details AS details
            JOIN titan007_1x2_changes AS changes
              ON changes.match_id = details.match_id
             AND changes.company_id = 47
            WHERE details.scheduled_time ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}} \\d{{2}}:\\d{{2}}$'
              AND details.scheduled_time::TIMESTAMP >= (%s::DATE - INTERVAL '3 hours')
              AND details.scheduled_time::TIMESTAMP < (%s::DATE + INTERVAL '1 day')
              AND ({status_filter_sql})
              AND changes.change_time ~ '^\\d{{1,2}}-\\d{{1,2}} \\d{{1,2}}:\\d{{2}}$'
        ),
        company_rows AS (
            SELECT
                company_rows_with_raw_time.*,
                CASE
                    WHEN raw_change_at < scheduled_at - INTERVAL '180 days'
                        THEN raw_change_at + INTERVAL '1 year'
                    WHEN raw_change_at > scheduled_at + INTERVAL '180 days'
                        THEN raw_change_at - INTERVAL '1 year'
                    ELSE raw_change_at
                END AS change_at
            FROM company_rows_with_raw_time
        ),
        suspended_rows AS (
            SELECT
                company_rows.*,
                seq - ROW_NUMBER() OVER (
                    PARTITION BY match_id ORDER BY seq
                ) AS suspension_group
            FROM company_rows
            WHERE source_status = '滚'
              AND is_suspended
        ),
        suspension_runs AS (
            SELECT
                match_id,
                suspension_group,
                MIN(seq) AS start_seq,
                MAX(seq) AS end_seq,
                (ARRAY_AGG(change_time ORDER BY seq))[1] AS start_time,
                (ARRAY_AGG(change_time ORDER BY seq DESC))[1] AS last_time,
                (ARRAY_AGG(change_at ORDER BY seq))[1] AS start_at,
                (ARRAY_AGG(change_at ORDER BY seq DESC))[1] AS last_at
            FROM suspended_rows
            GROUP BY match_id, suspension_group
        ),
        qualifying_runs AS (
            SELECT
                suspension_runs.*,
                COALESCE(next_row.change_time, suspension_runs.last_time) AS end_time,
                COALESCE(next_row.change_at, suspension_runs.last_at) AS end_at,
                EXTRACT(
                    EPOCH FROM COALESCE(next_row.change_at, suspension_runs.last_at)
                    - suspension_runs.start_at
                ) / 60 AS duration_minutes
            FROM suspension_runs
            LEFT JOIN company_rows AS next_row
              ON next_row.match_id = suspension_runs.match_id
             AND next_row.seq = suspension_runs.end_seq + 1
            WHERE COALESCE(next_row.change_at, suspension_runs.last_at)
                    - suspension_runs.start_at >= INTERVAL '3 minutes'
        ),
        qualifying_match_ids AS (
            SELECT DISTINCT match_id
            FROM qualifying_runs
        ),
        suspension_time_points AS (
            SELECT
                distinct_time_points.match_id,
                JSONB_AGG(
                    JSONB_BUILD_OBJECT(
                        'change_time', distinct_time_points.change_time,
                        'match_minute', distinct_time_points.match_minute
                    )
                    ORDER BY distinct_time_points.first_seq
                ) AS points
            FROM (
                SELECT
                    suspended_rows.match_id,
                    suspended_rows.change_time,
                    suspended_rows.match_minute,
                    MIN(suspended_rows.seq) AS first_seq
                FROM suspended_rows
                JOIN qualifying_match_ids USING (match_id)
                GROUP BY
                    suspended_rows.match_id,
                    suspended_rows.change_time,
                    suspended_rows.match_minute
            ) AS distinct_time_points
            GROUP BY distinct_time_points.match_id
        )
        SELECT
            details.match_id,
            details.league,
            details.scheduled_time,
            details.status_text,
            details.home_team,
            details.home_score,
            details.away_score,
            details.away_team,
            COALESCE(pb_status.status, '') AS pb_status,
            suspension_time_points.points
        FROM qualifying_match_ids
        JOIN match_details AS details USING (match_id)
        LEFT JOIN match_web_pb_status AS pb_status USING (match_id)
        JOIN suspension_time_points USING (match_id)
        ORDER BY details.scheduled_time ASC, details.match_id ASC
    """

    with psycopg2.connect(database_url) as connection:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute(query, (match_date, match_date))
            rows = cursor.fetchall()

    return [
        {
            "match_id": int(row[0]),
            "league": row[1],
            "scheduled_time": row[2],
            "status_text": row[3],
            "home_team": row[4],
            "home_score": row[5],
            "away_score": row[6],
            "away_team": row[7],
            "pb_status": row[8],
            "suspension_points": row[9],
        }
        for row in rows
    ]


class MatchWebApp:
    def __init__(
        self,
        database_url: str,
        users: Dict[str, str],
        secret: bytes,
        users_path: Optional[Path] = None,
        monitor_url: str = DEFAULT_MONITOR_URL,
    ):
        self.database_url = database_url
        self.users = users
        self.secret = secret
        self.users_path = users_path
        self.monitor_url = monitor_url.rstrip("/")
        self.users_lock = threading.Lock()

    def authenticate(self, username: str, password: str) -> bool:
        password_hash = self.users.get(username)
        return bool(password_hash and verify_password(password, password_hash))

    def create_session(self, username: str) -> str:
        expires_at = datetime.now(SHANGHAI) + SESSION_LIFETIME
        session_id = secrets.token_urlsafe(32)
        if username != ADMIN_USERNAME:
            self._replace_active_session(username, session_id, expires_at)
        payload = json.dumps(
            {
                "username": username,
                "expires": int(expires_at.timestamp()),
                "session_id": session_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).hexdigest().encode("ascii")
        return (encoded + b"." + signature).decode("ascii")

    def session_username(self, token: str) -> Optional[str]:
        try:
            encoded, supplied_signature = token.encode("ascii").rsplit(b".", 1)
            expected_signature = hmac.new(
                self.secret, encoded, hashlib.sha256
            ).hexdigest().encode("ascii")
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            padding = b"=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
            username = payload.get("username")
            if (
                username in self.users
                and int(payload.get("expires", 0))
                > int(datetime.now(SHANGHAI).timestamp())
            ):
                if username != ADMIN_USERNAME:
                    session_id = payload.get("session_id")
                    if not isinstance(session_id, str) or not self._active_session_matches(
                        str(username), session_id
                    ):
                        return None
                return str(username)
            return None
        except (
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            psycopg2.Error,
        ):
            return None

    def valid_session(self, token: str) -> bool:
        return self.session_username(token) is not None

    def list_usernames(self) -> List[str]:
        with self.users_lock:
            return sorted(self.users, key=str.casefold)

    def add_user(self, username: str, password: str) -> None:
        username = validate_username(username)
        with self.users_lock:
            if username in self.users:
                raise ValueError("该用户名已存在")
            self.users[username] = hash_password(password)
            self._save_users()

    def reset_user_password(self, username: str, password: str) -> None:
        username = validate_username(username)
        with self.users_lock:
            if username not in self.users:
                raise ValueError("用户不存在")
            self.users[username] = hash_password(password)
            self._save_users()

    def delete_user(self, username: str) -> None:
        username = validate_username(username)
        if username == ADMIN_USERNAME:
            raise ValueError("不能删除 admin 管理员账号")
        with self.users_lock:
            if username not in self.users:
                raise ValueError("用户不存在")
            del self.users[username]
            self._save_users()

    def set_pb_match_status(
        self, match_id: int, status: str, updated_by: str
    ) -> None:
        set_pb_match_status(self.database_url, match_id, status, updated_by)

    def _replace_active_session(
        self, username: str, session_id: str, expires_at: datetime
    ) -> None:
        replace_active_session(self.database_url, username, session_id, expires_at)

    def _active_session_matches(self, username: str, session_id: str) -> bool:
        return active_session_matches(self.database_url, username, session_id)

    def revoke_active_session(self, username: str) -> None:
        if username != ADMIN_USERNAME:
            delete_active_session(self.database_url, username)

    def _save_users(self) -> None:
        if self.users_path is None:
            raise RuntimeError("未配置账号文件")
        save_users(self.users_path, self.users)


class MatchWebServer(ThreadingHTTPServer):
    app: MatchWebApp

    def __init__(self, address: Tuple[str, int], app: MatchWebApp):
        self.app = app
        super().__init__(address, MatchWebHandler)


class MatchWebHandler(BaseHTTPRequestHandler):
    server: MatchWebServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            username = self.current_username()
            if username:
                self.redirect(PB_ONLY_PAGE if is_pb_only_username(username) else "/")
            else:
                self.serve_file(STATIC_DIR / "login.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/styles.css":
            self.serve_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/favicon.ico":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self.is_authenticated():
            if parsed.path.startswith("/api/") or parsed.path == "/monitor/api/status":
                self.send_json({"error": "未登录"}, HTTPStatus.UNAUTHORIZED)
            else:
                self.redirect("/login")
            return
        if is_pb_only_username(self.current_username()) and parsed.path not in PB_ONLY_PATHS:
            if parsed.path.startswith("/api/") or parsed.path == "/monitor/api/status":
                self.send_json({"error": "该账号仅可访问 PB 页面"}, HTTPStatus.FORBIDDEN)
            else:
                self.send_error(HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/":
            self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif parsed.path == "/company-47-suspensions":
            self.serve_file(
                STATIC_DIR / "company-47-suspensions.html",
                "text/html; charset=utf-8",
            )
        elif parsed.path == "/monitor":
            if not self.require_admin_page():
                return
            self.redirect("/monitor/")
        elif parsed.path == "/monitor/":
            if not self.require_admin_page():
                return
            self.proxy_monitor("/", is_api=False)
        elif parsed.path == "/monitor/api/status":
            if not self.require_admin_api():
                return
            self.proxy_monitor("/api/status", is_api=True)
        elif parsed.path == "/users":
            if not self.is_admin():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_file(STATIC_DIR / "users.html", "text/html; charset=utf-8")
        elif parsed.path == "/app.js":
            self.serve_file(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/company-47-suspensions.js":
            self.serve_file(
                STATIC_DIR / "company-47-suspensions.js",
                "text/javascript; charset=utf-8",
            )
        elif parsed.path == "/users.js":
            if not self.is_admin():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_file(STATIC_DIR / "users.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/api/matches":
            self.handle_matches(parsed.query)
        elif parsed.path == "/api/company-47-suspensions":
            self.handle_company_47_suspensions(parsed.query)
        elif parsed.path == "/api/session":
            username = self.current_username()
            self.send_json({"username": username, "is_admin": username == ADMIN_USERNAME})
        elif parsed.path == "/api/users":
            if not self.require_admin_api():
                return
            usernames = self.server.app.list_usernames()
            self.send_json(
                {
                    "users": [
                        {"username": name, "is_admin": name == ADMIN_USERNAME}
                        for name in usernames
                    ],
                    "total": len(usernames),
                }
            )
        elif parsed.path == "/logout":
            username = self.current_username()
            try:
                if username:
                    self.server.app.revoke_active_session(username)
            except psycopg2.Error:
                self.send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "暂时无法退出登录",
                )
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/login")
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
            self.end_headers()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/users":
            if not self.require_admin_api():
                return
            payload = self.read_json()
            if payload is None:
                return
            try:
                self.server.app.add_user(
                    str(payload.get("username", "")), str(payload.get("password", ""))
                )
            except (ValueError, RuntimeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"message": "用户已创建"}, HTTPStatus.CREATED)
            return
        if parsed.path != "/login":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        if not self.server.app.authenticate(username, password):
            self.redirect("/login?error=1")
            return
        try:
            session_token = self.server.app.create_session(username)
        except psycopg2.Error:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "暂时无法登录")
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header(
            "Location", PB_ONLY_PAGE if is_pb_only_username(username) else "/"
        )
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={session_token}; Path=/; "
            "HttpOnly; SameSite=Lax; Max-Age=43200",
        )
        self.end_headers()

    def do_PUT(self) -> None:
        pb_match_id = self.pb_status_api_match_id()
        if pb_match_id is not None:
            username = self.current_username()
            if username is None:
                self.send_json({"error": "未登录"}, HTTPStatus.UNAUTHORIZED)
                return
            payload = self.read_json()
            if payload is None:
                return
            try:
                self.server.app.set_pb_match_status(
                    pb_match_id, str(payload.get("status", "")), username
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except psycopg2.Error:
                self.send_json(
                    {"error": "暂时无法保存 PB 状态"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self.send_json({"match_id": pb_match_id, "status": payload["status"]})
            return
        username = self.user_api_username()
        if username is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self.require_admin_api():
            return
        payload = self.read_json()
        if payload is None:
            return
        try:
            self.server.app.reset_user_password(
                username, str(payload.get("password", ""))
            )
        except (ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"message": "密码已重置"})

    def do_DELETE(self) -> None:
        username = self.user_api_username()
        if username is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self.require_admin_api():
            return
        try:
            self.server.app.delete_user(username)
        except (ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"message": "用户已删除"})

    def user_api_username(self) -> Optional[str]:
        parsed = urlparse(self.path)
        prefix = "/api/users/"
        if not parsed.path.startswith(prefix):
            return None
        username = unquote(parsed.path[len(prefix):])
        return username if username and "/" not in username else None

    def pb_status_api_match_id(self) -> Optional[int]:
        parsed = urlparse(self.path)
        prefix = "/api/company-47-suspensions/"
        suffix = "/status"
        if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
            return None
        value = parsed.path[len(prefix):-len(suffix)]
        if not value.isdigit():
            return None
        return int(value)

    def read_json(self) -> Optional[Dict[str, object]]:
        if self.headers.get_content_type() != "application/json":
            self.send_json({"error": "请求格式必须是 JSON"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                raise ValueError
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "请求内容无效"}, HTTPStatus.BAD_REQUEST)
            return None

    def handle_matches(self, query: str) -> None:
        params = parse_qs(query)
        match_date = params.get("date", [today_in_shanghai()])[0]
        statuses = params.get("status", list(DEFAULT_STATUSES))
        odds_filter_value = params.get("odds_filter", ["1"])[0]
        if odds_filter_value not in {"0", "1"}:
            self.send_json({"error": "赔率筛选参数无效"}, HTTPStatus.BAD_REQUEST)
            return
        odds_filter = odds_filter_value == "1"
        try:
            matches = fetch_matches(
                self.server.app.database_url, match_date, statuses, odds_filter
            )
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except psycopg2.Error:
            self.send_json({"error": "暂时无法读取比赛数据"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_json(
            {
                "date": match_date,
                "statuses": statuses,
                "odds_filter": odds_filter,
                "total": len(matches),
                "refreshed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                "matches": matches,
            }
        )

    def handle_company_47_suspensions(self, query: str) -> None:
        params = parse_qs(query)
        match_date = params.get("date", [today_in_shanghai()])[0]
        statuses = params.get("status", list(DEFAULT_STATUSES))
        try:
            matches = fetch_company_47_suspensions(
                self.server.app.database_url, match_date, statuses
            )
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except psycopg2.Error:
            self.send_json({"error": "暂时无法读取比赛数据"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_json(
            {
                "date": match_date,
                "statuses": statuses,
                "minimum_duration_minutes": 3,
                "company_id": 47,
                "market": "胜平负",
                "total": len(matches),
                "refreshed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                "matches": matches,
            }
        )

    def is_authenticated(self) -> bool:
        return self.current_username() is not None

    def current_username(self) -> Optional[str]:
        if hasattr(self, "_cached_current_username"):
            return self._cached_current_username
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        username = self.server.app.session_username(morsel.value) if morsel else None
        self._cached_current_username = username
        return username

    def is_admin(self) -> bool:
        return self.current_username() == ADMIN_USERNAME

    def require_admin_api(self) -> bool:
        if not self.is_authenticated():
            self.send_json({"error": "未登录"}, HTTPStatus.UNAUTHORIZED)
            return False
        if not self.is_admin():
            self.send_json({"error": "仅 admin 可访问"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def require_admin_page(self) -> bool:
        if not self.is_admin():
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        return True

    def proxy_monitor(self, upstream_path: str, *, is_api: bool) -> None:
        request = Request(
            f"{self.server.app.monitor_url}{upstream_path}",
            headers={"Accept": "application/json" if is_api else "text/html"},
        )
        try:
            with urlopen(request, timeout=MONITOR_TIMEOUT_SECONDS) as response:
                body = response.read()
                content_type = response.headers.get(
                    "Content-Type",
                    "application/json; charset=utf-8"
                    if is_api
                    else "text/html; charset=utf-8",
                )
        except (HTTPError, URLError, TimeoutError, OSError):
            if is_api:
                self.send_json(
                    {"error": "采集监控暂不可用"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            else:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "采集监控暂不可用")
            return
        self.send_body(body, content_type)

    def serve_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_body(body, content_type)

    def send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write("[MatchWeb] " + (fmt % args) + "\n")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动比赛数据展示网页")
    parser.add_argument("--host", default=os.environ.get("MATCH_WEB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MATCH_WEB_PORT", "8082"))
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_environment()
    args = parse_args(argv)
    database_url = os.environ.get("SIMPLE_CRAWLER_DATABASE_URL", "").strip()
    monitor_url = os.environ.get("MATCH_WEB_MONITOR_URL", DEFAULT_MONITOR_URL).strip()
    users_path_text = os.environ.get("MATCH_WEB_USERS_FILE", "").strip()
    users_path = Path(users_path_text).expanduser() if users_path_text else APP_DIR / "users.json"
    if not users_path.is_absolute():
        users_path = REPO_DIR / users_path
    try:
        users = load_users(users_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not database_url:
        print(
            "请配置 SIMPLE_CRAWLER_DATABASE_URL。",
            file=sys.stderr,
        )
        return 2
    if not users:
        print("请先运行 python3 MatchWeb/manage_users.py add <账号> 创建账号。", file=sys.stderr)
        return 2
    try:
        ensure_pb_status_table(database_url)
        ensure_user_session_table(database_url)
    except psycopg2.Error:
        print("无法初始化 MatchWeb 数据表。", file=sys.stderr)
        return 2
    secret_text = os.environ.get("MATCH_WEB_SESSION_SECRET", "")
    secret = secret_text.encode("utf-8") if secret_text else secrets.token_bytes(32)
    if not secret_text:
        print("[MatchWeb] 未配置会话密钥；本次启动已使用临时随机密钥。")
    app = MatchWebApp(
        database_url,
        users,
        secret,
        users_path,
        monitor_url or DEFAULT_MONITOR_URL,
    )
    server = MatchWebServer((args.host, args.port), app)
    print(f"[MatchWeb] http://{args.host}:{server.server_address[1]}/")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
