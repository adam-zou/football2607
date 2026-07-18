#!/usr/bin/env python3
"""Add, remove, or list MatchWeb login accounts."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
from typing import Optional, Sequence

from dotenv import load_dotenv

from auth import hash_password, load_users, save_users, validate_username


APP_DIR = Path(__file__).resolve().parent


def user_file() -> Path:
    load_dotenv(APP_DIR / ".env")
    configured = os.environ.get("MATCH_WEB_USERS_FILE", "").strip()
    path = Path(configured).expanduser() if configured else APP_DIR / "users.json"
    return path if path.is_absolute() else APP_DIR.parent / path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="管理 MatchWeb 登录账号")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add = subparsers.add_parser("add", help="新增账号或重设已有账号密码")
    add.add_argument("username", help="登录账号")
    remove = subparsers.add_parser("remove", help="删除账号")
    remove.add_argument("username", help="登录账号")
    subparsers.add_parser("list", help="列出账号")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    path = user_file()
    users = load_users(path)

    if args.command == "list":
        if not users:
            print("当前没有账号")
        else:
            for username in sorted(users):
                print(username)
        return 0

    username = validate_username(args.username)
    if args.command == "remove":
        if username not in users:
            print(f"账号不存在：{username}")
            return 1
        del users[username]
        save_users(path, users)
        print(f"已删除账号：{username}")
        return 0

    password = getpass.getpass("密码：")
    confirmation = getpass.getpass("再次输入密码：")
    if password != confirmation:
        print("两次输入的密码不一致")
        return 1
    users[username] = hash_password(password)
    save_users(path, users)
    print(f"已保存账号：{username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
