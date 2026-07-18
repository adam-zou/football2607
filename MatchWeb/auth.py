"""Password hashing and file-backed user management for MatchWeb."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Dict


HASH_NAME = "sha256"
HASH_ITERATIONS = 310_000
SALT_BYTES = 16


def validate_username(username: str) -> str:
    username = username.strip()
    if not username or len(username) > 64 or any(char.isspace() for char in username):
        raise ValueError("账号必须为 1-64 个不含空格的字符")
    return username


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        HASH_NAME, password.encode("utf-8"), salt, HASH_ITERATIONS
    )
    return f"pbkdf2_{HASH_NAME}${HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != f"pbkdf2_{HASH_NAME}":
            return False
        digest = hashlib.pbkdf2_hmac(
            HASH_NAME,
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def load_users(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        users = payload["users"]
        if payload.get("version") != 1 or not isinstance(users, dict):
            raise ValueError
        return {
            validate_username(str(username)): str(password_hash)
            for username, password_hash in users.items()
        }
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise ValueError(f"账号文件格式无效：{path}") from exc


def save_users(path: Path, users: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(
        {"version": 1, "users": dict(sorted(users.items()))},
        ensure_ascii=False,
        indent=2,
    )
    temporary.write_text(data + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
