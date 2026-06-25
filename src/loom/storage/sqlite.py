"""SQLite storage backend for Loom.

Persists routing decisions, per-request metrics, session rollups, and the compression
cache. The schema is versioned via the ``schema_version`` table and applied idempotently
by :meth:`LoomStorage.migrate`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

SCHEMA_VERSION = 2


class LoomStorage:
    def __init__(self, db_path: str = "loom.db") -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

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
        self._create_tables()
        self.migrate()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

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
                    pass  # column already exists

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
        self.conn.commit()

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
        self.conn.commit()

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
        self.conn.commit()
