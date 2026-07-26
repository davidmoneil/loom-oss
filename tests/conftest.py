"""Shared fixtures: parametrized storage backend (SQLite always, Postgres
when reachable).

Set LOOM_TEST_POSTGRES_DSN to run the Postgres side locally, e.g.:

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=loom postgres:16
    LOOM_TEST_POSTGRES_DSN=postgresql://postgres:loom@localhost:5433/postgres pytest

CI provides the DSN via a service container, so the Postgres path always runs
there. Locally, without the env var, Postgres tests are skipped.
"""

import os
import uuid

import pytest

from loom.storage.sqlite import LoomStorage

POSTGRES_DSN = os.environ.get("LOOM_TEST_POSTGRES_DSN", "")


def _postgres_storage():
    from loom.storage.postgres import PostgresStorage

    return PostgresStorage(dsn=POSTGRES_DSN)


@pytest.fixture(params=["sqlite", "postgres"])
def storage(request, tmp_path):
    """A connected, migrated storage backend; the test body is backend-agnostic."""
    if request.param == "sqlite":
        backend = LoomStorage(str(tmp_path / "test.db"))
    else:
        if not POSTGRES_DSN:
            pytest.skip("LOOM_TEST_POSTGRES_DSN not set")
        pytest.importorskip("psycopg")
        backend = _postgres_storage()
    backend.connect()
    if request.param == "postgres":
        # Shared database across tests: scrub rows so each test starts clean.
        for table in (
            "metrics", "routing_decisions", "sessions", "gateway_keys",
            "compression_cache", "content_importance", "rate_limits",
        ):
            try:
                backend.conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
    try:
        yield backend
    finally:
        backend.close()


@pytest.fixture()
def unique_id():
    return uuid.uuid4().hex[:12]
