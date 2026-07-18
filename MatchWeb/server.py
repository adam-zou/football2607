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
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv

from auth import load_users, verify_password


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


def fetch_matches(
    database_url: str,
    match_date: str,
    status: str,
    odds_filter: bool = True,
) -> List[Dict[str, object]]:
    """Return matching rows from the crawler database without changing it."""

    match_date = validate_date(match_date)
    if status not in ALLOWED_STATUSES:
        raise ValueError("比赛状态无效")

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
                  AND handicap.source_status <> '滚'
                  AND (handicap.home_odds < 0.700 OR handicap.away_odds < 0.700)
                UNION
                SELECT totals.company_id, totals.change_time
                FROM titan007_over_under_changes AS totals
                WHERE totals.match_id = details.match_id
                  AND totals.source_status <> '滚'
                  AND totals.over_odds < 0.700
            ) AS matched
        ) AS filter_hits ON TRUE
        WHERE details.scheduled_time ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}} \\d{{2}}:\\d{{2}}$'
          AND details.scheduled_time::TIMESTAMP::DATE = %s::DATE
          AND {STATUS_SQL[status]}
          {odds_filter_sql}
        ORDER BY details.scheduled_time ASC, details.match_id ASC
    """

    with psycopg2.connect(database_url) as connection:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute(query, (match_date,))
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


class MatchWebApp:
    def __init__(self, database_url: str, users: Dict[str, str], secret: bytes):
        self.database_url = database_url
        self.users = users
        self.secret = secret

    def authenticate(self, username: str, password: str) -> bool:
        password_hash = self.users.get(username)
        return bool(password_hash and verify_password(password, password_hash))

    def create_session(self, username: str) -> str:
        payload = json.dumps(
            {
                "username": username,
                "expires": int((datetime.now(SHANGHAI) + SESSION_LIFETIME).timestamp()),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).hexdigest().encode("ascii")
        return (encoded + b"." + signature).decode("ascii")

    def valid_session(self, token: str) -> bool:
        try:
            encoded, supplied_signature = token.encode("ascii").rsplit(b".", 1)
            expected_signature = hmac.new(
                self.secret, encoded, hashlib.sha256
            ).hexdigest().encode("ascii")
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return False
            padding = b"=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
            return (
                payload.get("username") in self.users
                and int(payload.get("expires", 0)) > int(datetime.now(SHANGHAI).timestamp())
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False


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
            if self.is_authenticated():
                self.redirect("/")
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
            if parsed.path.startswith("/api/"):
                self.send_json({"error": "未登录"}, HTTPStatus.UNAUTHORIZED)
            else:
                self.redirect("/login")
            return
        if parsed.path == "/":
            self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif parsed.path == "/app.js":
            self.serve_file(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/api/matches":
            self.handle_matches(parsed.query)
        elif parsed.path == "/logout":
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
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={self.server.app.create_session(username)}; Path=/; "
            "HttpOnly; SameSite=Lax; Max-Age=43200",
        )
        self.end_headers()

    def handle_matches(self, query: str) -> None:
        params = parse_qs(query)
        match_date = params.get("date", [today_in_shanghai()])[0]
        status = params.get("status", ["进行中"])[0]
        odds_filter_value = params.get("odds_filter", ["1"])[0]
        if odds_filter_value not in {"0", "1"}:
            self.send_json({"error": "赔率筛选参数无效"}, HTTPStatus.BAD_REQUEST)
            return
        odds_filter = odds_filter_value == "1"
        try:
            matches = fetch_matches(
                self.server.app.database_url, match_date, status, odds_filter
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
                "status": status,
                "odds_filter": odds_filter,
                "total": len(matches),
                "refreshed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                "matches": matches,
            }
        )

    def is_authenticated(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return bool(morsel and self.server.app.valid_session(morsel.value))

    def serve_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
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
    secret_text = os.environ.get("MATCH_WEB_SESSION_SECRET", "")
    secret = secret_text.encode("utf-8") if secret_text else secrets.token_bytes(32)
    if not secret_text:
        print("[MatchWeb] 未配置会话密钥；本次启动已使用临时随机密钥。")
    app = MatchWebApp(database_url, users, secret)
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
