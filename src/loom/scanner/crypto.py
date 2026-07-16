"""Session-scoped Fernet encryption for the sensitive data scanner.

Auto-generates a unique encryption key per session. Keys are stored in
Postgres when available (loom_dlp_keys table) with configurable TTL.
Falls back to in-memory storage when Postgres is unavailable.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from loom.logging_setup import get_logger

logger = get_logger("loom.scanner.crypto")

KEY_TTL_HOURS = int(os.environ.get("LOOM_DLP_KEY_TTL_HOURS", "24"))

_conn_lock = threading.Lock()
_conn = None
_table_created = False
_pg_available = False
_pg_dsn: Optional[str] = None

_memory_keys: dict[str, bytes] = {}
_memory_lock = threading.Lock()


def configure(dsn: Optional[str] = None) -> None:
    global _pg_dsn
    _pg_dsn = dsn


def _get_conn():
    global _conn, _pg_available
    if _pg_dsn is None:
        return None
    with _conn_lock:
        if _conn is None or _conn.closed:
            import psycopg

            _conn = psycopg.connect(_pg_dsn, autocommit=True)
            _pg_available = True
        return _conn


def _ensure_table() -> bool:
    global _table_created
    if _table_created:
        return True
    if _pg_dsn is None:
        return False
    try:
        conn = _get_conn()
        if conn is None:
            return False
        conn.execute("""
            CREATE TABLE IF NOT EXISTS loom_dlp_keys (
                session_id  TEXT PRIMARY KEY,
                fernet_key  BYTEA NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours'
            )
        """)
        _table_created = True
        return True
    except Exception as e:
        logger.debug("crypto: table creation failed (falling back to memory): %s", e)
        return False


def _get_or_create_key(session_id: str) -> bytes:
    from cryptography.fernet import Fernet

    if _ensure_table():
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT fernet_key FROM loom_dlp_keys WHERE session_id = %s AND expires_at > NOW()",
                (session_id,),
            ).fetchone()
            if row:
                return bytes(row[0])

            key = Fernet.generate_key()
            conn.execute(
                """
                INSERT INTO loom_dlp_keys (session_id, fernet_key, expires_at)
                VALUES (%s, %s, NOW() + make_interval(hours => %s))
                ON CONFLICT (session_id) DO UPDATE SET
                    fernet_key = EXCLUDED.fernet_key,
                    expires_at = EXCLUDED.expires_at
                """,
                (session_id, key, KEY_TTL_HOURS),
            )
            return key
        except Exception as e:
            logger.debug("crypto: Postgres key lookup failed: %s", e)

    with _memory_lock:
        if session_id not in _memory_keys:
            _memory_keys[session_id] = Fernet.generate_key()
        return _memory_keys[session_id]


def encrypt(plaintext: str, session_id: str) -> str:
    from cryptography.fernet import Fernet

    key = _get_or_create_key(session_id)
    f = Fernet(key)
    token = f.encrypt(plaintext.encode()).decode()
    return f"[ENCRYPTED:{token}]"


def decrypt(encrypted_token: str, session_id: str) -> Optional[str]:
    if not encrypted_token.startswith("[ENCRYPTED:") or not encrypted_token.endswith("]"):
        return None

    from cryptography.fernet import Fernet

    token = encrypted_token[len("[ENCRYPTED:"):-1]
    key = _get_or_create_key(session_id)
    try:
        f = Fernet(key)
        return f.decrypt(token.encode()).decode()
    except Exception as e:
        logger.warning("crypto: decryption failed for session %s: %s", session_id, e)
        return None


def cleanup_expired() -> int:
    if not _ensure_table():
        return 0
    try:
        conn = _get_conn()
        result = conn.execute("DELETE FROM loom_dlp_keys WHERE expires_at < NOW()")
        return result.rowcount
    except Exception:
        return 0


def is_available() -> bool:
    return _pg_available
