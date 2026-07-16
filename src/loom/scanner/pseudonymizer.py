"""Session-scoped pseudonymization for the sensitive data scanner.

Generates consistent fake data for each detected value within a session.
The same real value always maps to the same fake value in a given session,
but different sessions produce different mappings.

Mappings are stored in Postgres (loom_pseudonym_map table) with 24h TTL
when available. Falls back to in-memory dict otherwise.
"""

from __future__ import annotations

import hashlib
from loom.logging_setup import get_logger
import threading
from typing import Optional

logger = get_logger("loom.scanner.pseudonymizer")

_conn_lock = threading.Lock()
_conn = None
_table_created = False
_pg_dsn: Optional[str] = None

_memory_maps: dict[str, dict[str, str]] = {}
_memory_lock = threading.Lock()


def configure(dsn: Optional[str] = None) -> None:
    global _pg_dsn
    _pg_dsn = dsn


def _get_conn():
    global _conn
    if _pg_dsn is None:
        return None
    with _conn_lock:
        if _conn is None or _conn.closed:
            import psycopg

            _conn = psycopg.connect(_pg_dsn, autocommit=True)
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
            CREATE TABLE IF NOT EXISTS loom_pseudonym_map (
                session_id   TEXT NOT NULL,
                rule_name    TEXT NOT NULL,
                real_hash    TEXT NOT NULL,
                fake_value   TEXT NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at   TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
                PRIMARY KEY (session_id, rule_name, real_hash)
            )
        """)
        _table_created = True
        return True
    except Exception as e:
        logger.debug("pseudonymizer: table creation failed: %s", e)
        return False


def _hash_value(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


_FAKE_GENERATORS = {
    "ssn": lambda seed: f"{(seed % 899) + 100:03d}-{(seed % 99) + 1:02d}-{(seed % 9999) + 1:04d}",
    "credit_card": lambda seed: f"****-****-****-{(seed % 9999) + 1:04d}",
    "email": lambda seed: f"user{seed % 10000}@example.com",
    "phone_us": lambda seed: f"(555) 000-{(seed % 9999) + 1:04d}",
    "ip_address": lambda seed: f"198.51.100.{(seed % 254) + 1}",
}


def _generate_fake(rule_name: str, real_value: str, session_id: str) -> str:
    seed_input = f"{session_id}:{real_value}"
    seed = int(hashlib.sha256(seed_input.encode()).hexdigest()[:8], 16)
    generator = _FAKE_GENERATORS.get(rule_name)
    if generator:
        return generator(seed)
    return f"[PSEUDO:{rule_name}:{seed % 10000}]"


def pseudonymize(
    real_value: str,
    rule_name: str,
    session_id: str,
) -> str:
    real_hash = _hash_value(real_value)

    if _ensure_table():
        try:
            conn = _get_conn()
            row = conn.execute(
                """SELECT fake_value FROM loom_pseudonym_map
                   WHERE session_id = %s AND rule_name = %s AND real_hash = %s
                   AND expires_at > NOW()""",
                (session_id, rule_name, real_hash),
            ).fetchone()
            if row:
                return row[0]

            fake = _generate_fake(rule_name, real_value, session_id)
            conn.execute(
                """INSERT INTO loom_pseudonym_map (session_id, rule_name, real_hash, fake_value)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (session_id, rule_name, real_hash) DO NOTHING""",
                (session_id, rule_name, real_hash, fake),
            )
            return fake
        except Exception as e:
            logger.debug("pseudonymizer: Postgres failed: %s", e)

    with _memory_lock:
        key = f"{session_id}:{rule_name}"
        if key not in _memory_maps:
            _memory_maps[key] = {}
        session_map = _memory_maps[key]

        if real_hash not in session_map:
            session_map[real_hash] = _generate_fake(rule_name, real_value, session_id)
        return session_map[real_hash]


def cleanup_expired() -> int:
    if not _ensure_table():
        return 0
    try:
        conn = _get_conn()
        result = conn.execute("DELETE FROM loom_pseudonym_map WHERE expires_at < NOW()")
        return result.rowcount
    except Exception:
        return 0
