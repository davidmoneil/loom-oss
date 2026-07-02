"""Postgres storage backend for Loom.

Drop-in replacement for the SQLite :class:`LoomStorage` backend. Uses
``psycopg`` (v3) with ``autocommit=True`` and thread-safe connection management.
Tables are created on first use and migrate idempotently.

Requires ``psycopg[binary]`` (not included in core dependencies).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("loom.storage.postgres")

SCHEMA_VERSION = 3


class PostgresStorage:
    """Postgres storage backend implementing the same interface as LoomStorage."""

    def __init__(self, dsn: str = "postgresql://localhost:5432/loom") -> None:
        self.dsn = dsn
        self._conn = None
        self._conn_lock = threading.Lock()
        self._table_created = False

    # ------------------------------------------------------------------ lifecycle
    def _get_conn(self):
        with self._conn_lock:
            if self._conn is None or self._conn.closed:
                import psycopg

                self._conn = psycopg.connect(self.dsn, autocommit=True)
            return self._conn

    def connect(self) -> None:
        self._get_conn()
        self._create_tables()
        self.migrate()

    def close(self) -> None:
        with self._conn_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self):
        c = self._get_conn()
        if not self._table_created:
            self._create_tables()
            self.migrate()
        return c

    # ------------------------------------------------------------------ schema
    def _create_tables(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_decisions (
                id SERIAL PRIMARY KEY,
                timestamp DOUBLE PRECISION NOT NULL,
                request_id TEXT,
                source TEXT,
                task_type TEXT,
                model_recommended TEXT,
                model_used TEXT,
                routing_reason TEXT,
                determinism_score DOUBLE PRECISION,
                alternatives_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id SERIAL PRIMARY KEY,
                timestamp DOUBLE PRECISION NOT NULL,
                request_id TEXT,
                model TEXT,
                requested_model TEXT,
                provider TEXT,
                task_type TEXT,
                tokens_in INTEGER,
                tokens_out INTEGER,
                latency_ms DOUBLE PRECISION,
                cost_estimate DOUBLE PRECISION,
                compressed INTEGER,
                compression_ratio DOUBLE PRECISION,
                message_count INTEGER DEFAULT 0,
                source TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id SERIAL PRIMARY KEY,
                session_id TEXT UNIQUE,
                started_at DOUBLE PRECISION,
                ended_at DOUBLE PRECISION,
                request_count INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                total_cost DOUBLE PRECISION DEFAULT 0.0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compression_cache (
                id SERIAL PRIMARY KEY,
                content_hash TEXT,
                age_ratio DOUBLE PRECISION,
                compressed_text TEXT,
                tier TEXT,
                tokens_before INTEGER,
                tokens_after INTEGER,
                created_at DOUBLE PRECISION,
                hits INTEGER DEFAULT 0,
                expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '7 days'
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_compression_hash ON compression_cache (content_hash)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content_importance (
                content_hash TEXT PRIMARY KEY,
                source TEXT,
                hit_count INTEGER DEFAULT 1,
                last_seen DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics (timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_routing_timestamp ON routing_decisions (timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_metrics_request_id ON metrics (request_id)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at DOUBLE PRECISION
            )
        """)
        self._table_created = True

    def migrate(self) -> None:
        conn = self.conn
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row[0] if row and row[0] is not None else 0

        if current < 2:
            for col, typ in [
                ("requested_model", "TEXT"),
                ("task_type", "TEXT"),
                ("message_count", "INTEGER DEFAULT 0"),
                ("source", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE metrics ADD COLUMN IF NOT EXISTS {col} {typ}")
                except Exception:
                    pass

        if current < 3:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS content_importance (
                    content_hash TEXT PRIMARY KEY,
                    source TEXT,
                    hit_count INTEGER DEFAULT 1,
                    last_seen DOUBLE PRECISION NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL
                )
            """)

        if current < SCHEMA_VERSION:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (%s, %s) "
                "ON CONFLICT (version) DO UPDATE SET applied_at = EXCLUDED.applied_at",
                (SCHEMA_VERSION, time.time()),
            )

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
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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

    # ------------------------------------------------------ content importance
    def record_content_importance(self, content_hash: str, source: str = "request") -> None:
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO content_importance (content_hash, source, hit_count, last_seen, created_at)
            VALUES (%s, %s, 1, %s, %s)
            ON CONFLICT (content_hash) DO UPDATE SET
                hit_count = content_importance.hit_count + 1,
                last_seen = %s
            """,
            (content_hash, source, now, now, now),
        )

    def get_content_importance(self, content_hashes: list[str]) -> dict[str, float]:
        if not content_hashes:
            return {}
        placeholders = ",".join("%s" for _ in content_hashes)
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
            age_hours = (now - row[2]) / 3600
            hits = row[1]
            if age_hours > 168:
                score = 0.3
            elif hits >= 3:
                score = 0.9
            elif hits >= 2:
                score = 0.75
            else:
                score = 0.6
            scores[row[0]] = score
        return scores

    def cleanup_stale_importance(self, max_age_hours: int = 168) -> int:
        cutoff = time.time() - max_age_hours * 3600
        result = self.conn.execute(
            "DELETE FROM content_importance WHERE last_seen < %s", (cutoff,)
        )
        return result.rowcount

    # ------------------------------------------------------------------ reads
    def get_routing_stats(self, hours: int = 24) -> dict:
        since = time.time() - hours * 3600
        rows = self.conn.execute(
            """
            SELECT model_used, COUNT(*) AS n
            FROM routing_decisions
            WHERE timestamp >= %s
            GROUP BY model_used
            ORDER BY n DESC
            """,
            (since,),
        ).fetchall()
        by_model = {r[0]: r[1] for r in rows}
        total = sum(by_model.values())

        metric_row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS n,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(cost_estimate), 0.0) AS cost,
                COALESCE(AVG(latency_ms), 0.0) AS avg_latency
            FROM metrics
            WHERE timestamp >= %s
            """,
            (since,),
        ).fetchone()

        return {
            "hours": hours,
            "total_decisions": total,
            "by_model": by_model,
            "request_count": metric_row[0] if metric_row else 0,
            "tokens_in": metric_row[1] if metric_row else 0,
            "tokens_out": metric_row[2] if metric_row else 0,
            "total_cost": metric_row[3] if metric_row else 0.0,
            "avg_latency_ms": metric_row[4] if metric_row else 0.0,
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

        def _bucket(row) -> dict:
            return {
                "requests": row[-5],
                "tokens_in": int(row[-4]),
                "tokens_out": int(row[-3]),
                "cost_usd": float(row[-2]),
                "tokens_saved": int(row[-1]),
            }

        t = self.conn.execute(
            f"SELECT {agg} FROM metrics WHERE timestamp >= %s", (since,)
        ).fetchone()
        totals = _bucket(t)

        by_model = [
            {"model": r[0], **_bucket(r)}
            for r in self.conn.execute(
                f"""
                SELECT COALESCE(model, 'unknown') AS model, {agg}
                FROM metrics WHERE timestamp >= %s
                GROUP BY model ORDER BY cost_usd DESC
                """,
                (since,),
            ).fetchall()
        ]
        by_source = [
            {"source": r[0], **_bucket(r)}
            for r in self.conn.execute(
                f"""
                SELECT COALESCE(source, 'unknown') AS source, {agg}
                FROM metrics WHERE timestamp >= %s
                GROUP BY source ORDER BY cost_usd DESC
                """,
                (since,),
            ).fetchall()
        ]
        by_day = [
            {"date": r[0], **_bucket(r)}
            for r in self.conn.execute(
                f"""
                SELECT to_char(to_timestamp(timestamp), 'YYYY-MM-DD') AS date, {agg}
                FROM metrics WHERE timestamp >= %s
                GROUP BY date ORDER BY date ASC
                """,
                (since,),
            ).fetchall()
        ]
        by_hour = [
            {"hour": r[0], "requests": r[1], "tokens_saved": int(r[2])}
            for r in self.conn.execute(
                f"""
                SELECT to_char(date_trunc('hour', to_timestamp(timestamp)),
                               'YYYY-MM-DD"T"HH24:00:00"Z"') AS hour,
                       COUNT(*) AS requests, {saved} AS tokens_saved
                FROM metrics WHERE timestamp >= %s
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
                CAST(timestamp / %s AS INTEGER) * %s AS bucket,
                COUNT(*) AS requests,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(cost_estimate), 0.0) AS cost,
                COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms
            FROM metrics
            WHERE timestamp >= %s
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (bucket_seconds, bucket_seconds, since),
        ).fetchall()
        buckets = [
            {
                "ts": int(r[0]),
                "requests": r[1],
                "tokens_in": r[2],
                "tokens_out": r[3],
                "cost": round(r[4], 6),
                "avg_latency_ms": round(r[5], 2),
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
            WHERE timestamp >= %s
            GROUP BY model
            ORDER BY requests DESC
            """,
            (since,),
        ).fetchall()
        by_model = {
            (r[0] or "unknown"): {
                "requests": r[1],
                "tokens_in": r[2],
                "tokens_out": r[3],
                "cost": round(r[4], 6),
            }
            for r in model_rows
        }

        source_rows = self.conn.execute(
            """
            SELECT COALESCE(source, 'default') AS source,
                   COUNT(*) AS requests,
                   COALESCE(SUM(cost_estimate), 0.0) AS cost
            FROM metrics
            WHERE timestamp >= %s
            GROUP BY source
            ORDER BY requests DESC
            """,
            (since,),
        ).fetchall()
        by_source = {
            r[0]: {"requests": r[1], "cost": round(r[2], 6)}
            for r in source_rows
        }

        task_rows = self.conn.execute(
            """
            SELECT COALESCE(task_type, 'general') AS task_type, COUNT(*) AS n
            FROM metrics
            WHERE timestamp >= %s
            GROUP BY task_type
            ORDER BY n DESC
            """,
            (since,),
        ).fetchall()
        by_task_type = {r[0]: r[1] for r in task_rows}

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
            where.append("m.model = %s")
            params.append(model)
        if source:
            where.append("COALESCE(m.source, 'default') = %s")
            params.append(source)
        if status and status.lower() != "success":
            where.append("1 = 0")
        if search:
            like = f"%{search}%"
            where.append(
                "(m.request_id LIKE %s OR m.model LIKE %s OR m.requested_model LIKE %s "
                "OR r.routing_reason LIKE %s OR m.task_type LIKE %s)"
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
        ).fetchone()[0]

        rows = self.conn.execute(
            f"""
            SELECT
                m.timestamp,
                m.request_id,
                COALESCE(m.source, 'default') AS source,
                m.requested_model,
                m.model AS model_used,
                m.provider,
                m.task_type,
                m.tokens_in,
                m.tokens_out,
                m.latency_ms,
                m.cost_estimate,
                m.compressed,
                m.compression_ratio,
                r.routing_reason
            FROM metrics m
            LEFT JOIN routing_decisions r ON m.request_id = r.request_id
            {clause}
            ORDER BY m.timestamp DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        ).fetchall()

        entries = [
            {
                "timestamp": r[0],
                "request_id": r[1],
                "source": r[2],
                "requested_model": r[3] or "auto",
                "model_used": r[4],
                "provider": r[5],
                "task_type": r[6] or "general",
                "tokens_in": r[7] or 0,
                "tokens_out": r[8] or 0,
                "latency_ms": r[9] or 0.0,
                "cost_estimate": r[10] or 0.0,
                "routing_reason": r[13] or "",
                "compressed": bool(r[11]),
                "compression_ratio": r[12] if r[12] is not None else 1.0,
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
            UPDATE compression_cache SET hits = hits + 1
            WHERE content_hash = %s AND expires_at > NOW()
            RETURNING content_hash, age_ratio, compressed_text, tier,
                      tokens_before, tokens_after, created_at
            """,
            (content_hash,),
        ).fetchone()
        if row is None:
            return None
        return {
            "content_hash": row[0],
            "age_ratio": row[1],
            "compressed_text": row[2],
            "tier": row[3],
            "tokens_before": row[4],
            "tokens_after": row[5],
            "created_at": row[6],
        }

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
            INSERT INTO compression_cache
                (content_hash, age_ratio, compressed_text, tier,
                 tokens_before, tokens_after, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL '7 days')
            ON CONFLICT (content_hash) DO UPDATE SET
                compressed_text = EXCLUDED.compressed_text,
                tier = EXCLUDED.tier,
                tokens_before = EXCLUDED.tokens_before,
                tokens_after = EXCLUDED.tokens_after,
                age_ratio = EXCLUDED.age_ratio,
                expires_at = EXCLUDED.expires_at,
                hits = 0
            """,
            (content_hash, age_ratio, compressed, tier, tokens_before, tokens_after, time.time()),
        )

    def cleanup_expired_cache(self) -> int:
        result = self.conn.execute(
            "DELETE FROM compression_cache WHERE expires_at < NOW()"
        )
        return result.rowcount

    def cache_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT count(*), coalesce(sum(hits), 0), "
            "coalesce(sum(tokens_before - tokens_after), 0) "
            "FROM compression_cache WHERE expires_at > NOW()"
        ).fetchone()
        return {
            "entries": row[0],
            "total_hits": row[1],
            "total_tokens_saved": row[2],
        }
