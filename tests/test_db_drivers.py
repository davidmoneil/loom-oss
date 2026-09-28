"""Build-time start check regression test.

loom-oss talks to Postgres directly via ``psycopg`` (v3), not SQLAlchemy, so
there's no dialect string to get wrong the way pulse#4 did
(``postgresql://`` vs ``postgresql+psycopg2://``). The equivalent failure
mode here is the ``postgres`` extra silently not being installed in the
image, or the gateway entrypoint picking up an import-time dependency that
isn't part of the base install — either would only surface once a container
tries to actually start. This pins both, matching the Dockerfile's
build-start-check RUN step.
"""

from __future__ import annotations

import importlib

import pytest


def test_gateway_app_imports_without_side_effects():
    """The FastAPI app object must be importable with no DB/network access.

    ``create_app()`` runs at import time (``app = create_app()`` in
    loom.gateway.app), but storage.connect() happens in the app's lifespan
    handler, not at import — so this must succeed with no Postgres reachable
    and no LOOM_CONFIG file present.
    """
    module = importlib.import_module("loom.gateway.app")
    assert hasattr(module, "app")


def test_postgres_dsn_uses_a_scheme_psycopg_accepts():
    """Guard the DSN scheme psycopg.connect() is handed.

    psycopg (v3) parses a plain ``postgresql://`` (or ``postgres://``) URL
    natively. If this ever grows a SQLAlchemy-style ``+driver`` suffix
    (``postgresql+psycopg2://`` etc.), psycopg.connect() would reject it at
    runtime — only visible once a container tries to connect.
    """
    from loom.storage.postgres import PostgresStorage

    default_dsn = PostgresStorage.__init__.__defaults__[0]
    assert default_dsn.split("://", 1)[0] in ("postgresql", "postgres")
    assert "+" not in default_dsn.split("://", 1)[0]


def test_psycopg_importable_when_postgres_extra_installed():
    """If the postgres extra is installed, psycopg must actually import.

    Skips (rather than fails) when the extra isn't installed in this
    environment — the Dockerfile's build-start-check RUN step is what
    enforces this for images built with INSTALL_EXTRAS=postgres.
    """
    pytest.importorskip("psycopg", reason="postgres extra not installed in this environment")
    import psycopg  # noqa: F401
