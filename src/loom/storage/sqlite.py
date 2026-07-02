"""SQLite storage backend for Loom.

Persists routing decisions, per-request metrics, session rollups, and the compression
cache. The schema is versioned via the ``schema_version`` table and applied idempotently
by :meth:`LoomStorage.migrate`.

Thread-safety: All write operations are serialized through a threading.Lock.
WAL mode allows concurrent reads while writes are locked. Commits are batched
via a periodic flush to avoid blocking the async event loop on every write.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA_VERSION = 3

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
                source TEXT
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
            for idx in [
                "CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics (timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_routing_timestamp ON routing_decisions (timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_metrics_request_id ON metrics (request_id)",
            ]:
                try:
                    c.execute(idx)
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
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO metrics (
                    timestamp, request_id, model, requested_model, provider,
                    task_type, tokens_in, tokens_out,
                    latency_ms, cost_estimate, compressed, compression_ratio,
                    message_count, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    _TOKENS_SAVED_SQL = (
        "COALESCE(SUM(CASE WHEN compressed = 1 AND compression_ratio > 0 "
        "AND compression_ratio < 1 "
        "THEN CAST(tokens_in * (1.0 / compression_ratio - 1.0) AS INTEGER) "
        "ELSE 0 END), 0)"
    )

    def get_cost_summary(self, days: int = 30) -> dict:
        """Aggregates for the observability /api/costs contract.

        compression_ratio is tokens_after / tokens_before, so the saved-token
        estimate per row is tokens_in * (1/ratio - 1) for compressed rows.
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
            "hours": hours,
            "bucket_seconds": bucket_seconds,
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
    ) -> dict:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        where: list[str] = []
        params: list[Any] = []
        if model:
            where.append("m.model = ?")
            params.append(model)
        if source:
            where.append("COALESCE(m.source, 'default') = ?")
            params.append(source)
        if status and status.lower() != "success":
            where.append("1 = 0")
        if search:
            like = f"%{search}%"
            where.append(
                "(m.request_id LIKE ? OR m.model LIKE ? OR m.requested_model LIKE ? "
                "OR r.routing_reason LIKE ? OR m.task_type LIKE ?)"
            )
            params.extend([like, like, like, like, like])

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
                r.routing_reason   AS routing_reason
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
                "status": "success",
            }
            for r in rows
        ]

        return {"total": total, "offset": offset, "limit": limit, "entries": entries}

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
