"""
db.py
-----
Local SQLite persistence so credentials (.env) and Auto Trade settings are
remembered across app restarts, instead of resetting every time Streamlit
reruns / the server restarts.

Everything is stored as JSON under a simple key/value table in
`app_data.db`, next to this file.

⚠ NOTE: this is plain local storage, NOT encrypted. Fine for local/personal
use; if you deploy this publicly, don't rely on it to protect secrets.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import threading

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_data.db")
_lock = threading.Lock()

CREDENTIALS_KEY   = "credentials"
AUTO_CONFIG_KEY   = "auto_trade_config"
AUTO_TRADE_PW_KEY = "auto_trade_password"


def _connect():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings ("
        "key TEXT PRIMARY KEY, "
        "value TEXT NOT NULL"
        ")"
    )
    return conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.commit()
        conn.close()


def save_json(key: str, value) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        conn.commit()
        conn.close()


def load_json(key: str, default=None):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        conn.close()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        return default


def delete_key(key: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
        conn.close()


# ─── Convenience wrappers ───────────────────────────────────────────────────

def save_credentials(creds: dict) -> None:
    save_json(CREDENTIALS_KEY, creds)


def load_credentials() -> dict:
    return load_json(CREDENTIALS_KEY, {}) or {}


def clear_credentials() -> None:
    delete_key(CREDENTIALS_KEY)


def save_auto_config(config: dict) -> None:
    save_json(AUTO_CONFIG_KEY, config)


def load_auto_config() -> dict:
    return load_json(AUTO_CONFIG_KEY, {}) or {}


# ─── Auto Trade password lock ──────────────────────────────────────────────
# Password is never stored in plain text — only a salted PBKDF2 hash.

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()


def has_auto_trade_password() -> bool:
    return bool(load_json(AUTO_TRADE_PW_KEY, {}))


def save_auto_trade_password(password: str) -> None:
    salt = os.urandom(16)
    save_json(AUTO_TRADE_PW_KEY, {
        "salt": salt.hex(),
        "hash": _hash_password(password, salt),
    })


def verify_auto_trade_password(password: str) -> bool:
    data = load_json(AUTO_TRADE_PW_KEY, {})
    if not data or "salt" not in data or "hash" not in data:
        return False
    salt = bytes.fromhex(data["salt"])
    return hmac.compare_digest(_hash_password(password, salt), data["hash"])


def clear_auto_trade_password() -> None:
    delete_key(AUTO_TRADE_PW_KEY)
