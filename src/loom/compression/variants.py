"""Compressed-variant store — preserve originals for pointer resolution.

Ported from the internal Loom context graph: compressed text carries a
``<!--loom:compressed:<tier>:<hash>-->`` pointer tag; this store keeps the
pre-compression original keyed by that hash so the pointer can be resolved
later (audit, retrieval, or tier upgrade). It also answers "is this content
curated?" for relevance-aware compression: content indexed by an external
context engine (any ``LoomContent`` node not created by the gateway) is
high-signal and gets compressed less aggressively.

Graph schema (matches internal Loom's ``context/graph.py``):

    (c:LoomContent {content_hash, source, original_text, ...})
        -[:HAS_COMPRESSED]->
    (v:CompressedVariant {variant_id, tier, original_tokens,
                          compressed_tokens, text, content_hash})

Three backends:

- ``age`` — Apache AGE extension in Postgres (default when configured).
  Same Cypher schema, same Postgres instance as the storage backend.
  Install: ``pip install 'loom-gateway[postgres]'`` (uses psycopg).
- ``neo4j`` — standalone Neo4j server (legacy / advanced GDS use cases).
  Install: ``pip install 'loom-gateway[neo4j]'``.
- ``""`` (empty) — :class:`NullVariantStore`, no variant storage.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from loom.logging_setup import get_logger

log = get_logger("loom.variants")

_AGE_GRAPH = "loom_graph"


class NullVariantStore:
    """No-op store used when no variant backend is configured."""

    enabled = False

    def put_variant(
        self,
        content_hash: str,
        original_text: str,
        compressed_text: str,
        tier: str,
        tokens_before: int,
        tokens_after: int,
        source_hint: str = "",
    ) -> None:
        return None

    def get_original(self, content_hash: str) -> Optional[str]:
        return None

    def is_indexed(self, content_hash: str) -> bool:
        return False

    def close(self) -> None:
        return None


class AgeVariantStore:
    """Apache AGE (Postgres extension) backed variant store.

    Uses the same Cypher graph schema as :class:`Neo4jVariantStore` but
    runs inside Postgres via the AGE extension, eliminating the need for
    a separate Neo4j instance.  Shares the psycopg dependency with the
    Postgres storage backend.
    """

    enabled = True

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._dsn = dsn
        self._local = threading.local()
        conn = self._conn()
        self._ensure_age(conn)
        self._ensure_schema(conn)

    def _conn(self) -> Any:
        """Thread-local connection (AGE requires SET on each connection)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            return conn
        import psycopg

        conn = psycopg.connect(self._dsn, autocommit=True)
        conn.execute("SET search_path = ag_catalog, '$user', public")
        conn.execute("LOAD 'age'")
        self._local.conn = conn
        return conn

    def _ensure_age(self, conn: Any) -> None:
        row = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'age'"
        ).fetchone()
        if not row:
            try:
                conn.execute("CREATE EXTENSION IF NOT EXISTS age")
                log.info("installed AGE extension")
            except Exception as exc:
                raise RuntimeError(
                    "Apache AGE extension is not installed and could not be "
                    "created — install it or switch to variant_store=neo4j"
                ) from exc
        row = conn.execute(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s",
            (_AGE_GRAPH,),
        ).fetchone()
        if not row:
            conn.execute("SELECT create_graph(%s)", (_AGE_GRAPH,))
            log.info("created AGE graph '%s'", _AGE_GRAPH)

    def _ensure_schema(self, conn: Any) -> None:
        for label in ("LoomContent", "CompressedVariant"):
            try:
                conn.execute(
                    "SELECT create_vlabel(%s, %s)", (_AGE_GRAPH, label)
                )
            except Exception:
                pass  # already exists

    def _cypher(self, conn: Any, query: str, params: dict | None = None) -> list:
        """Run a Cypher query via AGE and return result rows."""
        if params:
            for key, val in params.items():
                if isinstance(val, str):
                    escaped = val.replace("\\", "\\\\").replace("'", "\\'")
                    query = query.replace(f"${key}", f"'{escaped}'")
                elif isinstance(val, (int, float)):
                    query = query.replace(f"${key}", str(val))
                elif val is None:
                    query = query.replace(f"${key}", "NULL")

        sql = f"SELECT * FROM cypher('{_AGE_GRAPH}', $$ {query} $$) AS (result agtype)"
        try:
            return conn.execute(sql).fetchall()
        except Exception:
            # Reconnect on stale connection and retry once.
            self._local.conn = None
            conn = self._conn()
            return conn.execute(sql).fetchall()

    def put_variant(
        self,
        content_hash: str,
        original_text: str,
        compressed_text: str,
        tier: str,
        tokens_before: int,
        tokens_after: int,
        source_hint: str = "",
    ) -> None:
        conn = self._conn()
        source_hint = source_hint or "text"
        variant_id = f"{content_hash}:{tier}"
        self._cypher(
            conn,
            """
            MERGE (c:LoomContent {content_hash: $content_hash})
            ON CREATE SET c.content_id = $content_hash,
                          c.source = 'gateway'
            SET c.original_text = $original_text,
                c.content_type = $source_hint
            RETURN c
            """,
            {
                "content_hash": content_hash,
                "original_text": original_text,
                "source_hint": source_hint,
            },
        )
        self._cypher(
            conn,
            """
            MERGE (v:CompressedVariant {variant_id: $variant_id})
            SET v.technique = 'graduated',
                v.content_type = $source_hint,
                v.tier = $tier,
                v.original_tokens = $tokens_before,
                v.compressed_tokens = $tokens_after,
                v.text = $compressed_text,
                v.content_hash = $content_hash
            RETURN v
            """,
            {
                "variant_id": variant_id,
                "source_hint": source_hint,
                "tier": tier,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "compressed_text": compressed_text,
                "content_hash": content_hash,
            },
        )
        self._cypher(
            conn,
            """
            MATCH (c:LoomContent {content_hash: $content_hash}),
                  (v:CompressedVariant {variant_id: $variant_id})
            MERGE (c)-[:HAS_COMPRESSED]->(v)
            RETURN c
            """,
            {"content_hash": content_hash, "variant_id": variant_id},
        )

    def get_original(self, content_hash: str) -> Optional[str]:
        conn = self._conn()
        rows = self._cypher(
            conn,
            """
            MATCH (c:LoomContent {content_hash: $content_hash})
            RETURN c.original_text
            """,
            {"content_hash": content_hash},
        )
        if not rows:
            return None
        import json

        val = rows[0][0]
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val
        return val

    def is_indexed(self, content_hash: str) -> bool:
        """True when the content exists as curated (non-gateway) graph content."""
        conn = self._conn()
        rows = self._cypher(
            conn,
            """
            MATCH (c:LoomContent {content_hash: $content_hash})
            WHERE c.source <> 'gateway'
            RETURN c
            """,
            {"content_hash": content_hash},
        )
        return len(rows) > 0

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


class Neo4jVariantStore:
    """Neo4j-backed variant store.

    Originals captured by the gateway are stored on ``LoomContent`` nodes
    with ``source = 'gateway'`` so they are distinguishable from curated
    content written by a context engine — only the latter counts as
    "indexed" for relevance scoring.
    """

    enabled = True

    def __init__(
        self,
        uri: str,
        user: str = "",
        password: str = "",
        database: str = "neo4j",
    ) -> None:
        import neo4j  # optional dependency, imported lazily

        auth = (user, password) if user else None
        self._driver = neo4j.GraphDatabase.driver(uri, auth=auth)
        self._database = database
        self._driver.verify_connectivity()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        statements = [
            "CREATE CONSTRAINT compressed_variant_id IF NOT EXISTS "
            "FOR (v:CompressedVariant) REQUIRE v.variant_id IS UNIQUE",
            "CREATE INDEX loom_content_hash IF NOT EXISTS "
            "FOR (c:LoomContent) ON (c.content_hash)",
        ]
        with self._driver.session(database=self._database) as session:
            for stmt in statements:
                try:
                    session.run(stmt)
                except Exception as exc:  # older servers, permissions
                    log.debug("variant store schema statement failed: %s", exc)

    def put_variant(
        self,
        content_hash: str,
        original_text: str,
        compressed_text: str,
        tier: str,
        tokens_before: int,
        tokens_after: int,
        source_hint: str = "",
    ) -> None:
        variant_id = f"{content_hash}:{tier}"
        with self._driver.session(database=self._database) as session:
            session.run(
                """
                MERGE (c:LoomContent {content_hash: $content_hash})
                ON CREATE SET c.content_id = $content_hash,
                              c.source = 'gateway',
                              c.created_at = datetime()
                SET c.original_text = coalesce(c.original_text, $original_text),
                    c.content_type = coalesce(c.content_type, $source_hint)
                MERGE (v:CompressedVariant {variant_id: $variant_id})
                SET v.technique = 'graduated',
                    v.content_type = $source_hint,
                    v.tier = $tier,
                    v.original_tokens = $tokens_before,
                    v.compressed_tokens = $tokens_after,
                    v.text = $compressed_text,
                    v.content_hash = $content_hash,
                    v.created_at = coalesce(v.created_at, datetime()),
                    v.updated_at = datetime()
                MERGE (c)-[:HAS_COMPRESSED]->(v)
                """,
                content_hash=content_hash,
                original_text=original_text,
                compressed_text=compressed_text,
                tier=tier,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                source_hint=source_hint or "text",
                variant_id=variant_id,
            )

    def get_original(self, content_hash: str) -> Optional[str]:
        with self._driver.session(database=self._database) as session:
            record = session.run(
                """
                MATCH (c:LoomContent {content_hash: $content_hash})
                RETURN c.original_text AS original
                LIMIT 1
                """,
                content_hash=content_hash,
            ).single()
            return record["original"] if record else None

    def is_indexed(self, content_hash: str) -> bool:
        """True when the content exists as curated (non-gateway) graph content."""
        with self._driver.session(database=self._database) as session:
            record = session.run(
                """
                MATCH (c:LoomContent {content_hash: $content_hash})
                WHERE coalesce(c.source, '') <> 'gateway'
                RETURN 1 LIMIT 1
                """,
                content_hash=content_hash,
            ).single()
            return record is not None

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            pass


def create_variant_store(compression_cfg: Any) -> Any:
    """Build the configured variant store; never raises.

    Falls back to :class:`NullVariantStore` when the backend is unset, the
    required driver is not installed, or the server is unreachable — variant
    preservation is an enhancement, not a dependency of the request path.
    """
    backend = getattr(compression_cfg, "variant_store", "")

    if backend == "age":
        dsn = getattr(compression_cfg, "age_dsn", "")
        if not dsn:
            log.warning("compression.variant_store=age but age_dsn is empty")
            return NullVariantStore()
        try:
            store = AgeVariantStore(dsn)
            log.info("AGE variant store connected: %s", dsn.split("@")[-1])
            return store
        except Exception as exc:
            log.warning(
                "AGE variant store unavailable (%s) — falling back to null", exc
            )
            return NullVariantStore()

    if backend == "neo4j":
        uri = getattr(compression_cfg, "neo4j_uri", "")
        if not uri:
            log.warning("compression.variant_store=neo4j but neo4j_uri is empty")
            return NullVariantStore()
        try:
            store = Neo4jVariantStore(
                uri,
                user=getattr(compression_cfg, "neo4j_user", ""),
                password=getattr(compression_cfg, "neo4j_password", ""),
                database=getattr(compression_cfg, "neo4j_database", "neo4j")
                or "neo4j",
            )
            log.info("variant store connected: %s", uri)
            return store
        except Exception as exc:
            log.warning(
                "variant store unavailable (%s) — falling back to null", exc
            )
            return NullVariantStore()

    return NullVariantStore()
