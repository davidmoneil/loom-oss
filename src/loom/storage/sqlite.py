"""SQLite storage backend for Loom.

Persists routing decisions, per-request metrics, session rollups, and the compression
cache. The schema is versioned via the ``schema_version`` table and applied idempotently
by :meth:`LoomStorage.migrate`.

Thread-safety: All write operations are serialized through a threading.Lock.
WAL mode allows concurrent reads while writes are locked. Commits are batched
via a periodic flush to avoid blocking the async event loop on every write.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA_VERSION = 11

_FLUSH_INTERVAL_SECONDS = 2.0


class LoomStorage:
    def __init__(self, db_path: str = "loom.db") -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = threading.Lock()
        self._pending_writes = 0
        self._last_flush = 0.0
        self._flush_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        if self._conn is not None:
            return
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._create_tables()
        self.migrate()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self._conn is not None:
            with self._write_lock:
                self._conn.commit()
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def _flush(self) -> None:
        """Commit pending writes. Called by timer or explicitly."""
        with self._write_lock:
            if self._pending_writes > 0 and self._conn is not None:
                self._conn.commit()
                self._pending_writes = 0
                self._last_flush = time.monotonic()

    def _schedule_flush(self) -> None:
        """Schedule a deferred commit if writes are pending."""
        self._pending_writes += 1
        now = time.monotonic()
        if now - self._last_flush >= _FLUSH_INTERVAL_SECONDS:
            self._conn.commit()
            self._pending_writes = 0
            self._last_flush = now
        elif self._flush_timer is None or not self._flush_timer.is_alive():
            self._flush_timer = threading.Timer(_FLUSH_INTERVAL_SECONDS, self._flush)
            self._flush_timer.daemon = True
            self._flush_timer.start()

    # ------------------------------------------------------------------ schema
    def _create_tables(self) -> None:
        c = self._conn
        assert c is not None
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS routing_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                request_id TEXT,
                source TEXT,
                task_type TEXT,
                model_recommended TEXT,
                model_used TEXT,
                routing_reason TEXT,
                determinism_score REAL,
                alternatives_json TEXT
            );

            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                request_id TEXT,
                model TEXT,
                requested_model TEXT,
                provider TEXT,
                task_type TEXT,
                tokens_in INTEGER,
                tokens_out INTEGER,
                latency_ms REAL,
                cost_estimate REAL,
                compressed INTEGER,
                compression_ratio REAL,
                message_count INTEGER DEFAULT 0,
                source TEXT,
                status_code INTEGER DEFAULT 200
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE,
                started_at REAL,
                ended_at REAL,
                request_count INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                total_cost REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS compression_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT,
                age_ratio REAL,
                compressed_text TEXT,
                tier TEXT,
                tokens_before INTEGER,
                tokens_after INTEGER,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_compression_lookup
                ON compression_cache (content_hash, age_ratio);

            CREATE TABLE IF NOT EXISTS content_importance (
                content_hash TEXT PRIMARY KEY,
                source TEXT,
                hit_count INTEGER DEFAULT 1,
                last_seen REAL NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                request_id TEXT,
                provider TEXT NOT NULL,
                model TEXT,
                requests_limit INTEGER,
                requests_remaining INTEGER,
                tokens_limit INTEGER,
                tokens_remaining INTEGER,
                input_tokens_limit INTEGER,
                input_tokens_remaining INTEGER,
                output_tokens_limit INTEGER,
                output_tokens_remaining INTEGER,
                tokens_utilization REAL,
                input_tokens_utilization REAL,
                output_tokens_utilization REAL
            );
            CREATE INDEX IF NOT EXISTS idx_rate_limits_timestamp
                ON rate_limits (timestamp);
            CREATE INDEX IF NOT EXISTS idx_rate_limits_provider
                ON rate_limits (provider, timestamp);

            CREATE INDEX IF NOT EXISTS idx_metrics_timestamp
                ON metrics (timestamp);
            CREATE INDEX IF NOT EXISTS idx_routing_timestamp
                ON routing_decisions (timestamp);
            CREATE INDEX IF NOT EXISTS idx_metrics_request_id
                ON metrics (request_id);

            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL
            );
            """
        )
        c.commit()

    def migrate(self) -> None:
        c = self.conn
        row = c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] if row and row["v"] is not None else 0

        if current < 2:
            for col, typ in [
                ("requested_model", "TEXT"),
                ("task_type", "TEXT"),
                ("message_count", "INTEGER DEFAULT 0"),
                ("source", "TEXT"),
            ]:
                try:
                    c.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass

        if current < 3:
            try:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS content_importance (
                        content_hash TEXT PRIMARY KEY,
                        source TEXT,
                        hit_count INTEGER DEFAULT 1,
                        last_seen REAL NOT NULL,
                        created_at REAL NOT NULL
                    );
                """)
            except sqlite3.OperationalError:
                pass

        if current < 4:
            # Session tracking (contract /api/sessions): sessions gains the
            # calling source; ended_at doubles as last_seen, request_count as
            # the turn counter.
            try:
                c.execute("ALTER TABLE sessions ADD COLUMN source TEXT")
            except sqlite3.OperationalError:
                pass
            for idx in [
                "CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics (timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_routing_timestamp ON routing_decisions (timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_metrics_request_id ON metrics (request_id)",
            ]:
                try:
                    c.execute(idx)
                except sqlite3.OperationalError:
                    pass

        if current < 5:
            try:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS rate_limits (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        request_id TEXT,
                        provider TEXT NOT NULL,
                        model TEXT,
                        requests_limit INTEGER,
                        requests_remaining INTEGER,
                        tokens_limit INTEGER,
                        tokens_remaining INTEGER,
                        input_tokens_limit INTEGER,
                        input_tokens_remaining INTEGER,
                        output_tokens_limit INTEGER,
                        output_tokens_remaining INTEGER,
                        tokens_utilization REAL,
                        input_tokens_utilization REAL,
                        output_tokens_utilization REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_rate_limits_timestamp
                        ON rate_limits (timestamp);
                    CREATE INDEX IF NOT EXISTS idx_rate_limits_provider
                        ON rate_limits (provider, timestamp);
                """)
            except sqlite3.OperationalError:
                pass

        if current < 6:
            # Multi-signal session fingerprinting: add metadata columns to sessions.
            for col, typ in [
                ('client_type', 'TEXT'),
                ('user_id', 'TEXT'),
                ('api_key_suffix', 'TEXT'),
                ('system_hash', 'TEXT'),
            ]:
                try:
                    c.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass

        if current < 7:
            try:
                c.execute(
                    "ALTER TABLE metrics ADD COLUMN tokens_saved INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass

        if current < 8:
            try:
                c.execute("ALTER TABLE metrics ADD COLUMN session_id TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("CREATE INDEX IF NOT EXISTS idx_metrics_session_id ON metrics (session_id)")
            except sqlite3.OperationalError:
                pass

        if current < 9:
            try:
                c.execute(
                    "ALTER TABLE metrics ADD COLUMN status_code INTEGER DEFAULT 200"
                )
            except sqlite3.OperationalError:
                pass

        if current < 10:
            # Cache-aware cost analytics: persist the prompt-cache split and the
            # Claude Code skill/command a request belongs to.
            for col, typ in [
                ("cache_read_tokens", "INTEGER DEFAULT 0"),
                ("cache_creation_tokens", "INTEGER DEFAULT 0"),
                ("skill", "TEXT"),
            ]:
                try:
                    c.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            try:
                c.execute("CREATE INDEX IF NOT EXISTS idx_metrics_skill ON metrics (skill)")
            except sqlite3.OperationalError:
                pass

        if current < 11:
            # Gateway API keys — parity with the Postgres backend so key auth
            # works on the default install, not just Postgres deployments.
            try:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS gateway_keys (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        key_hash TEXT NOT NULL,
                        key_prefix TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        last_used_at REAL,
                        enabled INTEGER DEFAULT 1
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_keys_hash
                        ON gateway_keys (key_hash);
                """)
            except sqlite3.OperationalError:
                pass

        if current < SCHEMA_VERSION:
            c.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, time.time()),
            )
            c.commit()

    # ------------------------------------------------------------------ writes
    def record_routing_decision(
        self,
        request_id: str,
        source: str,
        task_type: str,
        model: str,
        reason: str,
        model_recommended: Optional[str] = None,
        determinism_score: Optional[float] = None,
        alternatives: Optional[list[Any]] = None,
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO routing_decisions (
                    timestamp, request_id, source, task_type,
                    model_recommended, model_used, routing_reason,
                    determinism_score, alternatives_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    request_id,
                    source,
                    task_type,
                    model_recommended if model_recommended is not None else model,
                    model,
                    reason,
                    determinism_score,
                    json.dumps(alternatives) if alternatives is not None else None,
                ),
            )
            self._schedule_flush()

    def record_metrics(
        self,
        request_id: str,
        model: str,
        provider: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        cost: float,
        compressed: bool = False,
        compression_ratio: float = 1.0,
        requested_model: Optional[str] = None,
        task_type: Optional[str] = None,
        message_count: int = 0,
        source: Optional[str] = None,
        tokens_saved: int = 0,
        session_id: Optional[str] = None,
        status_code: int = 200,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        skill: Optional[str] = None,
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO metrics (
                    timestamp, request_id, model, requested_model, provider,
                    task_type, tokens_in, tokens_out,
                    latency_ms, cost_estimate, compressed, compression_ratio,
                    message_count, source, tokens_saved, session_id,
                    status_code, cache_read_tokens, cache_creation_tokens, skill
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    request_id,
                    model,
                    requested_model,
                    provider,
                    task_type,
                    tokens_in,
                    tokens_out,
                    latency_ms,
                    cost,
                    1 if compressed else 0,
                    compression_ratio,
                    message_count,
                    source,
                    tokens_saved,
                    session_id,
                    status_code,
                    cache_read_tokens,
                    cache_creation_tokens,
                    skill,
                ),
            )
            self._schedule_flush()

    def record_rate_limits(
        self,
        request_id: str,
        provider: str,
        model: Optional[str] = None,
        ratelimit: Optional[dict] = None,
    ) -> None:
        if not ratelimit:
            return
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO rate_limits (
                    timestamp, request_id, provider, model,
                    requests_limit, requests_remaining,
                    tokens_limit, tokens_remaining,
                    input_tokens_limit, input_tokens_remaining,
                    output_tokens_limit, output_tokens_remaining,
                    tokens_utilization, input_tokens_utilization,
                    output_tokens_utilization
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    request_id,
                    provider,
                    model,
                    ratelimit.get("ratelimit_requests_limit"),
                    ratelimit.get("ratelimit_requests_remaining"),
                    ratelimit.get("ratelimit_tokens_limit"),
                    ratelimit.get("ratelimit_tokens_remaining"),
                    ratelimit.get("ratelimit_input_tokens_limit"),
                    ratelimit.get("ratelimit_input_tokens_remaining"),
                    ratelimit.get("ratelimit_output_tokens_limit"),
                    ratelimit.get("ratelimit_output_tokens_remaining"),
                    ratelimit.get("ratelimit_tokens_utilization"),
                    ratelimit.get("ratelimit_input_tokens_utilization"),
                    ratelimit.get("ratelimit_output_tokens_utilization"),
                ),
            )
            self._schedule_flush()

    # ------------------------------------------------------ content importance
    def record_content_importance(self, content_hash: str, source: str = "request") -> None:
        now = time.time()
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO content_importance (content_hash, source, hit_count, last_seen, created_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT (content_hash) DO UPDATE SET
                    hit_count = hit_count + 1,
                    last_seen = ?
                """,
                (content_hash, source, now, now, now),
            )
            self._schedule_flush()

    def get_content_importance(self, content_hashes: list[str]) -> dict[str, float]:
        if not content_hashes:
            return {}
        placeholders = ",".join("?" for _ in content_hashes)
        rows = self.conn.execute(
            f"""
            SELECT content_hash, hit_count, last_seen, created_at
            FROM content_importance
            WHERE content_hash IN ({placeholders})
            """,
            content_hashes,
        ).fetchall()
        scores: dict[str, float] = {}
        now = time.time()
        for row in rows:
            age_hours = (now - row["last_seen"]) / 3600
            hits = row["hit_count"]
            if age_hours > 168:
                score = 0.3
            elif hits >= 3:
                score = 0.9
            elif hits >= 2:
                score = 0.75
            else:
                score = 0.6
            scores[row["content_hash"]] = score
        return scores

    def cleanup_stale_importance(self, max_age_hours: int = 168) -> int:
        cutoff = time.time() - max_age_hours * 3600
        with self._write_lock:
            cursor = self.conn.execute(
                "DELETE FROM content_importance WHERE last_seen < ?", (cutoff,)
            )
            self._schedule_flush()
        return cursor.rowcount

    # ------------------------------------------------------------------ reads
    def get_routing_stats(self, hours: int = 24) -> dict:
        since = time.time() - hours * 3600
        rows = self.conn.execute(
            """
            SELECT model_used, COUNT(*) AS n
            FROM routing_decisions
            WHERE timestamp >= ?
            GROUP BY model_used
            ORDER BY n DESC
            """,
            (since,),
        ).fetchall()
        by_model = {r["model_used"]: r["n"] for r in rows}
        total = sum(by_model.values())

        metric_rows = self.conn.execute(
            """
            SELECT
                COUNT(*) AS n,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(cost_estimate), 0.0) AS cost,
                COALESCE(AVG(latency_ms), 0.0) AS avg_latency
            FROM metrics
            WHERE timestamp >= ?
            """,
            (since,),
        ).fetchone()

        return {
            "hours": hours,
            "total_decisions": total,
            "by_model": by_model,
            "request_count": metric_rows["n"] if metric_rows else 0,
            "tokens_in": metric_rows["tokens_in"] if metric_rows else 0,
            "tokens_out": metric_rows["tokens_out"] if metric_rows else 0,
            "total_cost": metric_rows["cost"] if metric_rows else 0.0,
            "avg_latency_ms": metric_rows["avg_latency"] if metric_rows else 0.0,
        }

    # ------------------------------------------------------------ sessions
    def touch_session(
        self,
        session_id: str,
        source: str = "",
        tokens: int = 0,
        cost: float = 0.0,
        *,
        client_type: str = "",
        user_id: str = "",
        api_key_suffix: str = "",
        system_hash: str = "",
    ) -> int:
        """Upsert a session row and increment its turn counter.

        Returns the turn number after the update (1 for a new session).
        ended_at doubles as last_seen; request_count is the turn counter.
        Optional keyword args store session metadata for dashboard display.
        """
        now = time.time()
        with self._write_lock:
            row = self.conn.execute(
                """
                INSERT INTO sessions
                    (session_id, source, started_at, ended_at,
                     request_count, total_tokens, total_cost,
                     client_type, user_id, api_key_suffix, system_hash)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    ended_at = excluded.ended_at,
                    source = COALESCE(excluded.source, sessions.source),
                    request_count = sessions.request_count + 1,
                    total_tokens = sessions.total_tokens + excluded.total_tokens,
                    total_cost = sessions.total_cost + excluded.total_cost,
                    client_type = COALESCE(excluded.client_type, sessions.client_type),
                    user_id = COALESCE(excluded.user_id, sessions.user_id),
                    api_key_suffix = COALESCE(excluded.api_key_suffix, sessions.api_key_suffix),
                    system_hash = COALESCE(excluded.system_hash, sessions.system_hash)
                RETURNING request_count
                """,
                (session_id, source, now, now, tokens, cost,
                 client_type or None, user_id or None,
                 api_key_suffix or None, system_hash or None),
            ).fetchone()
            self._schedule_flush()
        return int(row[0]) if row else 1

    def get_session_stats(self, hours: int | None = None) -> dict:
        sql = (
            "SELECT COUNT(*) AS sessions,"
            " COALESCE(SUM(request_count), 0) AS total_turns FROM sessions"
        )
        params: tuple = ()
        if hours is not None:
            sql += " WHERE ended_at >= ?"
            params = (time.time() - hours * 3600,)
        row = self.conn.execute(sql, params).fetchone()
        return {"sessions": row["sessions"], "total_turns": row["total_turns"]}

    def list_sessions(self, hours: int = 24, limit: int = 200) -> list[dict]:
        cutoff = time.time() - hours * 3600
        rows = self.conn.execute(
            """
            SELECT session_id, COALESCE(source, 'unknown') AS source,
                   request_count AS turns, ended_at AS last_seen,
                   client_type, user_id, api_key_suffix, system_hash
            FROM sessions WHERE ended_at >= ?
            ORDER BY ended_at DESC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_routing_decisions(self, hours: int = 24, limit: int = 200) -> dict:
        since = time.time() - hours * 3600
        rows = self.conn.execute(
            """
            SELECT timestamp, request_id, source, task_type,
                   model_recommended, model_used, routing_reason,
                   determinism_score, alternatives_json
            FROM routing_decisions
            WHERE timestamp >= ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (since, limit),
        ).fetchall()
        entries = []
        for r in rows:
            entry = dict(r)
            if entry.get("alternatives_json"):
                try:
                    entry["alternatives"] = json.loads(entry["alternatives_json"])
                except Exception:
                    entry["alternatives"] = []
            else:
                entry["alternatives"] = []
            entry.pop("alternatives_json", None)
            entries.append(entry)

        reason_rows = self.conn.execute(
            """
            SELECT routing_reason, COUNT(*) AS n
            FROM routing_decisions
            WHERE timestamp >= ?
            GROUP BY routing_reason
            ORDER BY n DESC
            """,
            (since,),
        ).fetchall()
        by_reason = {r["routing_reason"]: r["n"] for r in reason_rows}

        override_rows = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM routing_decisions
            WHERE timestamp >= ? AND model_recommended != model_used
            """,
            (since,),
        ).fetchone()

        return {
            "hours": hours,
            "total": len(entries),
            "entries": entries,
            "by_reason": by_reason,
            "overrides": override_rows["n"] if override_rows else 0,
        }

    # Measured savings recorded at compression time. (The old derivation
    # tokens_in * (1/ratio - 1) was wrong — tokens_in is the provider-reported
    # POST-compression count.)
    _TOKENS_SAVED_SQL = "COALESCE(SUM(COALESCE(tokens_saved, 0)), 0)"

    def get_cost_summary(self, days: int = 30) -> dict:
        """Aggregates for the observability /api/costs contract.

        tokens_saved sums the per-request measured savings recorded by the
        gateway at compression time.
        """
        since = time.time() - days * 86400
        hour_since = time.time() - 24 * 3600
        saved = self._TOKENS_SAVED_SQL

        agg = (
            "COUNT(*) AS requests, "
            "COALESCE(SUM(tokens_in), 0) AS tokens_in, "
            "COALESCE(SUM(tokens_out), 0) AS tokens_out, "
            "COALESCE(SUM(cost_estimate), 0.0) AS cost_usd, "
            f"{saved} AS tokens_saved"
        )

        totals = dict(
            self.conn.execute(
                f"SELECT {agg} FROM metrics WHERE timestamp >= ?", (since,)
            ).fetchone()
        )

        by_model = [
            dict(r)
            for r in self.conn.execute(
                f"""
                SELECT COALESCE(model, 'unknown') AS model, {agg}
                FROM metrics WHERE timestamp >= ?
                GROUP BY model ORDER BY cost_usd DESC
                """,
                (since,),
            ).fetchall()
        ]
        by_source = [
            dict(r)
            for r in self.conn.execute(
                f"""
                SELECT COALESCE(source, 'unknown') AS source, {agg}
                FROM metrics WHERE timestamp >= ?
                GROUP BY source ORDER BY cost_usd DESC
                """,
                (since,),
            ).fetchall()
        ]
        by_day = [
            dict(r)
            for r in self.conn.execute(
                f"""
                SELECT date(timestamp, 'unixepoch') AS date, {agg}
                FROM metrics WHERE timestamp >= ?
                GROUP BY date ORDER BY date ASC
                """,
                (since,),
            ).fetchall()
        ]
        by_hour = [
            dict(r)
            for r in self.conn.execute(
                f"""
                SELECT strftime('%Y-%m-%dT%H:00:00Z', timestamp, 'unixepoch') AS hour,
                       COUNT(*) AS requests, {saved} AS tokens_saved
                FROM metrics WHERE timestamp >= ?
                GROUP BY hour ORDER BY hour ASC
                """,
                (hour_since,),
            ).fetchall()
        ]

        return {
            "window_days": days,
            "totals": totals,
            "by_model": by_model,
            "by_source": by_source,
            "by_tier": [],
            "by_day": by_day,
            "by_hour": by_hour,
        }

    def get_metrics_timeseries(
        self, hours: int = 24, bucket_seconds: int = 3600
    ) -> dict:
        bucket_seconds = max(int(bucket_seconds), 1)
        since = time.time() - hours * 3600

        bucket_rows = self.conn.execute(
            """
            SELECT
                CAST(timestamp / ? AS INTEGER) * ? AS bucket,
                COUNT(*) AS requests,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(cost_estimate), 0.0) AS cost,
                COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms
            FROM metrics
            WHERE timestamp >= ?
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (bucket_seconds, bucket_seconds, since),
        ).fetchall()
        buckets = [
            {
                "ts": int(r["bucket"]),
                "requests": r["requests"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "cost": round(r["cost"], 6),
                "avg_latency_ms": round(r["avg_latency_ms"], 2),
            }
            for r in bucket_rows
        ]

        model_rows = self.conn.execute(
            """
            SELECT model,
                   COUNT(*) AS requests,
                   COALESCE(SUM(tokens_in), 0) AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(cost_estimate), 0.0) AS cost
            FROM metrics
            WHERE timestamp >= ?
            GROUP BY model
            ORDER BY requests DESC
            """,
            (since,),
        ).fetchall()
        by_model = {
            (r["model"] or "unknown"): {
                "requests": r["requests"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "cost": round(r["cost"], 6),
            }
            for r in model_rows
        }

        source_rows = self.conn.execute(
            """
            SELECT COALESCE(source, 'default') AS source,
                   COUNT(*) AS requests,
                   COALESCE(SUM(cost_estimate), 0.0) AS cost
            FROM metrics
            WHERE timestamp >= ?
            GROUP BY source
            ORDER BY requests DESC
            """,
            (since,),
        ).fetchall()
        by_source = {
            r["source"]: {"requests": r["requests"], "cost": round(r["cost"], 6)}
            for r in source_rows
        }

        task_rows = self.conn.execute(
            """
            SELECT COALESCE(task_type, 'general') AS task_type, COUNT(*) AS n
            FROM metrics
            WHERE timestamp >= ?
            GROUP BY task_type
            ORDER BY n DESC
            """,
            (since,),
        ).fetchall()
        by_task_type = {r["task_type"]: r["n"] for r in task_rows}

        return {
            "window_hours": hours,
            "interval_minutes": bucket_seconds // 60,
            "buckets": buckets,
            "by_model": by_model,
            "by_source": by_source,
            "by_task_type": by_task_type,
        }

    def get_audit_entries(
        self,
        limit: int = 50,
        offset: int = 0,
        model: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        skill: Optional[str] = None,
    ) -> dict:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        where: list[str] = []
        params: list[Any] = []
        if model:
            where.append("m.model = ?")
            params.append(model)
        if skill:
            where.append("m.skill = ?")
            params.append(skill)
        if source:
            where.append("COALESCE(m.source, 'default') = ?")
            params.append(source)
        if status:
            if status.lower() == "error":
                where.append("m.status_code >= 400")
            elif status.lower() == "success":
                where.append("(m.status_code < 400 OR m.status_code IS NULL)")
        if search:
            like = f"%{search}%"
            where.append(
                "(m.request_id LIKE ? OR m.model LIKE ? OR m.requested_model LIKE ? "
                "OR r.routing_reason LIKE ? OR m.task_type LIKE ? OR m.session_id LIKE ?)"
            )
            params.extend([like, like, like, like, like, like])

        clause = (" WHERE " + " AND ".join(where)) if where else ""

        total = self.conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM metrics m
            LEFT JOIN routing_decisions r ON m.request_id = r.request_id
            {clause}
            """,
            params,
        ).fetchone()["n"]

        rows = self.conn.execute(
            f"""
            SELECT
                m.timestamp        AS timestamp,
                m.request_id       AS request_id,
                COALESCE(m.source, 'default') AS source,
                m.requested_model  AS requested_model,
                m.model            AS model_used,
                m.provider         AS provider,
                m.task_type        AS task_type,
                m.tokens_in        AS tokens_in,
                m.tokens_out       AS tokens_out,
                m.latency_ms       AS latency_ms,
                m.cost_estimate    AS cost_estimate,
                m.compressed       AS compressed,
                m.compression_ratio AS compression_ratio,
                r.routing_reason   AS routing_reason,
                m.session_id       AS session_id,
                m.status_code      AS status_code,
                m.cache_read_tokens AS cache_read_tokens,
                m.cache_creation_tokens AS cache_creation_tokens,
                m.skill            AS skill
            FROM metrics m
            LEFT JOIN routing_decisions r ON m.request_id = r.request_id
            {clause}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

        entries = [
            {
                "timestamp": r["timestamp"],
                "request_id": r["request_id"],
                "source": r["source"],
                "requested_model": r["requested_model"] or "auto",
                "model_used": r["model_used"],
                "provider": r["provider"],
                "task_type": r["task_type"] or "general",
                "tokens_in": r["tokens_in"] or 0,
                "tokens_out": r["tokens_out"] or 0,
                "latency_ms": r["latency_ms"] or 0.0,
                "cost_estimate": r["cost_estimate"] or 0.0,
                "routing_reason": r["routing_reason"] or "",
                "compressed": bool(r["compressed"]),
                "compression_ratio": r["compression_ratio"]
                if r["compression_ratio"] is not None
                else 1.0,
                "status": "error" if (r["status_code"] or 200) >= 400 else "success",
                "session_id": r["session_id"] if r["session_id"] else None,
                "cache_read_tokens": r["cache_read_tokens"] or 0,
                "cache_creation_tokens": r["cache_creation_tokens"] or 0,
                "skill": r["skill"] if r["skill"] else None,
            }
            for r in rows
        ]

        return {"total": total, "offset": offset, "limit": limit, "entries": entries}

    # ------------------------------------------------------------------ rate limits
    def get_rate_limit_current(self, provider: str = "anthropic") -> Optional[dict]:
        row = self.conn.execute(
            """
            SELECT timestamp, request_id, provider, model,
                   requests_limit, requests_remaining,
                   tokens_limit, tokens_remaining,
                   input_tokens_limit, input_tokens_remaining,
                   output_tokens_limit, output_tokens_remaining,
                   tokens_utilization, input_tokens_utilization,
                   output_tokens_utilization
            FROM rate_limits
            WHERE provider = ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (provider,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_rate_limit_trend(
        self, hours: int = 48, provider: str = "anthropic"
    ) -> list[dict]:
        since = time.time() - hours * 3600
        rows = self.conn.execute(
            """
            SELECT
                strftime('%Y-%m-%dT%H:00:00Z', timestamp, 'unixepoch') AS hour,
                AVG(tokens_utilization) AS avg_tokens_util,
                AVG(input_tokens_utilization) AS avg_input_util,
                AVG(output_tokens_utilization) AS avg_output_util,
                MAX(tokens_utilization) AS max_tokens_util,
                MIN(tokens_remaining) AS min_tokens_remaining,
                COUNT(*) AS samples
            FROM rate_limits
            WHERE timestamp >= ? AND provider = ?
            GROUP BY hour
            ORDER BY hour ASC
            """,
            (since, provider),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ compression cache
    def get_compression_cached(
        self, content_hash: str, age_ratio: float
    ) -> Optional[dict]:
        row = self.conn.execute(
            """
            SELECT content_hash, age_ratio, compressed_text, tier,
                   tokens_before, tokens_after, created_at
            FROM compression_cache
            WHERE content_hash = ?
            ORDER BY ABS(age_ratio - ?) ASC, created_at DESC
            LIMIT 1
            """,
            (content_hash, age_ratio),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def put_compression_cached(
        self,
        content_hash: str,
        age_ratio: float,
        compressed: str,
        tier: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO compression_cache (
                    content_hash, age_ratio, compressed_text, tier,
                    tokens_before, tokens_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_hash,
                    age_ratio,
                    compressed,
                    tier,
                    tokens_before,
                    tokens_after,
                    time.time(),
                ),
            )
            self._schedule_flush()

    # ---- gateway key management ----

    @staticmethod
    def _hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def create_gateway_key(self, name: str) -> dict:
        """Create a new gateway key. Returns the full key ONCE."""
        raw_key = f"loom-{secrets.token_urlsafe(32)}"
        key_hash = self._hash_key(raw_key)
        prefix = raw_key[:12]
        now = time.time()
        with self._write_lock:
            cur = self.conn.execute(
                "INSERT INTO gateway_keys (name, key_hash, key_prefix, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, key_hash, prefix, now),
            )
            self.conn.commit()
        return {
            "id": cur.lastrowid,
            "name": name,
            "key": raw_key,
            "key_prefix": prefix,
            "created_at": now,
        }

    def validate_gateway_key(self, raw_key: str) -> Optional[dict]:
        """Validate a key. Returns key info if valid, None if not."""
        key_hash = self._hash_key(raw_key)
        row = self.conn.execute(
            "SELECT id, name, key_prefix, enabled FROM gateway_keys "
            "WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        if not row or not row[3]:
            return None
        with self._write_lock:
            self.conn.execute(
                "UPDATE gateway_keys SET last_used_at = ? WHERE id = ?",
                (time.time(), row[0]),
            )
            self._schedule_flush()
        return {"id": row[0], "name": row[1], "key_prefix": row[2]}

    def list_gateway_keys(self) -> list[dict]:
        """List all keys (masked — prefix only, no full key)."""
        rows = self.conn.execute(
            "SELECT id, name, key_prefix, created_at, last_used_at, enabled "
            "FROM gateway_keys ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "key_preview": f"{r[2]}...",
                "created_at": r[3],
                "last_used_at": r[4],
                "enabled": bool(r[5]),
            }
            for r in rows
        ]

    def toggle_gateway_key(self, key_id: int, enabled: bool) -> bool:
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE gateway_keys SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, key_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def delete_gateway_key(self, key_id: int) -> bool:
        with self._write_lock:
            cur = self.conn.execute(
                "DELETE FROM gateway_keys WHERE id = ?",
                (key_id,),
            )
            self.conn.commit()
        return cur.rowcount > 0
