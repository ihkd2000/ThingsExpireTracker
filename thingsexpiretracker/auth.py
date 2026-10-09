"""Users, roles, passwords and login sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Optional

ROLES = ("viewer", "editor", "admin")
_RANK = {role: n for n, role in enumerate(ROLES)}
MIN_PASSWORD = 10
SESSION_HOURS = 12
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class AuthError(Exception):
    pass


def allows(role: str, needed: str) -> bool:
    return _RANK.get(role, -1) >= _RANK[needed]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use a password of at least {MIN_PASSWORD} characters.")


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str) -> dict[str, Any]:
    username = username.strip()
    if not username or len(username) > 60 or any(c.isspace() for c in username):
        raise AuthError("Usernames are 1-60 characters with no spaces.")
    if role not in ROLES:
        raise AuthError("Role must be one of: " + ", ".join(ROLES) + ".")
    _check_password(password)
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, _now()),
        )
    except sqlite3.IntegrityError:
        raise AuthError(f"The user '{username}' already exists.") from None
    conn.commit()
    return get_user(conn, cursor.lastrowid)


def get_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT id, username, role, disabled, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("No such user.")
    return {**dict(row), "disabled": bool(row["disabled"])}


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, username, role, disabled, created_at FROM users ORDER BY username").fetchall()
    return [{**dict(r), "disabled": bool(r["disabled"])} for r in rows]


def has_users(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def _active_admins(conn: sqlite3.Connection, excluding: int = 0) -> int:
    return conn.execute("SELECT count(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?", (excluding,)).fetchone()[0]


def update_user(conn: sqlite3.Connection, user_id: int, *, role: Optional[str] = None, disabled: Optional[bool] = None,
                password: Optional[str] = None) -> dict[str, Any]:
    user = get_user(conn, user_id)
    new_role = role if role is not None else user["role"]
    new_disabled = user["disabled"] if disabled is None else disabled
    if new_role not in ROLES:
        raise AuthError("Role must be one of: " + ", ".join(ROLES) + ".")
    if (new_role != "admin" or new_disabled) and user["role"] == "admin" and not user["disabled"] and _active_admins(conn, user_id) == 0:
        raise AuthError("There must be at least one active admin.")
    if password is not None:
        _check_password(password)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    conn.execute("UPDATE users SET role = ?, disabled = ? WHERE id = ?", (new_role, 1 if new_disabled else 0, user_id))
    if new_disabled or password is not None:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    return get_user(conn, user_id)


# ---- sessions --------------------------------------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def login(conn: sqlite3.Connection, username: str, password: str) -> tuple[str, dict[str, Any]]:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    # Always run the hash so a wrong username costs the same time as a wrong password.
    stored = row["password_hash"] if row else hash_password("not-a-real-password")
    ok = verify_password(password, stored) and row is not None and not row["disabled"]
    if not ok:
        raise AuthError("Wrong username or password.")
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=SESSION_HOURS)).replace(microsecond=0).isoformat(sep=" ")
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)", (_token_hash(token), row["id"], expires))
    conn.commit()
    return token, get_user(conn, row["id"])


def user_for_token(conn: sqlite3.Connection, token: str) -> Optional[dict[str, Any]]:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.id FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at >= ? AND u.disabled = 0",
        (_token_hash(token), _now()),
    ).fetchone()
    return get_user(conn, row["id"]) if row else None


def logout(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()


class LoginThrottle:
    """Slows password guessing: 5 failures for a username locks it for a minute (in memory, per process)."""

    def __init__(self, limit: int = 5, window: float = 60.0, clock=time.monotonic):
        self._limit, self._window, self._clock = limit, window, clock
        self._fails: dict[str, list[float]] = {}

    def blocked(self, key: str) -> bool:
        now = self._clock()
        recent = [t for t in self._fails.get(key.lower(), []) if now - t < self._window]
        self._fails[key.lower()] = recent
        return len(recent) >= self._limit

    def failed(self, key: str) -> None:
        self._fails.setdefault(key.lower(), []).append(self._clock())

    def succeeded(self, key: str) -> None:
        self._fails.pop(key.lower(), None)
