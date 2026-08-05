"""Auth middleware: fail-open without keys, default-protected with keys."""

import pytest
from fastapi.testclient import TestClient

from loom.storage.sqlite import LoomStorage


@pytest.fixture()
def gw_client(tmp_path):
    """TestClient with a real SQLite storage injected into gateway state.

    The module-level app is shared across the test session, so storage and
    the keys-exist cache are restored afterwards.
    """
    from loom.gateway.app import app

    gw = app.state.gateway
    prev_storage = gw.storage
    prev_cache = getattr(gw, "_gateway_keys_exist", None)
    storage = LoomStorage(str(tmp_path / "auth-test.db"))
    storage.connect()
    gw.storage = storage
    gw._gateway_keys_exist = None
    try:
        yield TestClient(app), gw, storage
    finally:
        storage.close()
        gw.storage = prev_storage
        gw._gateway_keys_exist = prev_cache


def test_no_keys_fail_open_but_reported(gw_client):
    client, gw, storage = gw_client
    resp = client.get("/api/config")
    assert resp.status_code == 200
    health = client.get("/health").json()
    assert health["auth_enabled"] is False


def test_with_keys_api_requires_auth(gw_client):
    client, gw, storage = gw_client
    key = storage.create_gateway_key("test")["key"]
    gw._gateway_keys_exist = None

    # Unauthenticated: config read and mutation both rejected.
    assert client.get("/api/config").status_code == 401
    assert client.patch(
        "/api/config/compression", json={"enabled": True}
    ).status_code == 401
    assert client.get("/api/audit?limit=1").status_code == 401

    # Authenticated via the dedicated header.
    ok = client.get("/api/config", headers={"x-loom-gateway-key": key})
    assert ok.status_code == 200

    # Public allowlist stays open.
    assert client.get("/health").status_code == 200
    assert client.get("/api/models").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/health").json()["auth_enabled"] is True


def test_with_keys_disabled_key_rejected(gw_client):
    client, gw, storage = gw_client
    created = storage.create_gateway_key("test")
    gw._gateway_keys_exist = None
    storage.toggle_gateway_key(created["id"], False)
    resp = client.get(
        "/api/config", headers={"x-loom-gateway-key": created["key"]}
    )
    assert resp.status_code == 401


def test_oauth_passthrough_never_covers_admin_api(gw_client):
    client, gw, storage = gw_client
    storage.create_gateway_key("test")
    gw._gateway_keys_exist = None
    prev = gw.config.server.oauth_passthrough
    gw.config.server.oauth_passthrough = True
    try:
        resp = client.get(
            "/api/config",
            headers={"Authorization": "Bearer sk-ant-oat01-abcdef"},
        )
        assert resp.status_code == 401
    finally:
        gw.config.server.oauth_passthrough = prev


def test_sqlite_gateway_key_roundtrip(tmp_path):
    storage = LoomStorage(str(tmp_path / "keys.db"))
    storage.connect()
    try:
        created = storage.create_gateway_key("laptop")
        assert created["key"].startswith("loom-")
        assert storage.validate_gateway_key(created["key"])["name"] == "laptop"
        assert storage.validate_gateway_key("loom-wrong") is None

        listed = storage.list_gateway_keys()
        assert len(listed) == 1
        assert "key" not in listed[0]  # full key never listed
        assert listed[0]["enabled"] is True

        assert storage.toggle_gateway_key(created["id"], False)
        assert storage.validate_gateway_key(created["key"]) is None
        assert storage.delete_gateway_key(created["id"])
        assert storage.list_gateway_keys() == []
    finally:
        storage.close()
