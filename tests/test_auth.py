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


# --- storage outage: fail closed, reconnect (2026-11-09 incident) ----------


@pytest.fixture()
def gw_storage_down(gw_client):
    """Gateway whose storage failed to connect at startup."""
    client, gw, storage = gw_client
    prev = (gw.storage, gw.storage_unavailable, gw.storage_error)
    gw.storage = None
    gw.storage_unavailable = True
    gw.storage_error = "OperationalError"
    try:
        yield client, gw
    finally:
        gw.storage, gw.storage_unavailable, gw.storage_error = prev


def test_storage_unavailable_fails_closed(gw_storage_down):
    client, gw = gw_storage_down
    for path in ("/api/config", "/api/audit?limit=1", "/api/costs"):
        resp = client.get(path)
        assert resp.status_code == 503, path
        assert resp.headers.get("retry-after") == "5"
        assert resp.json()["error"]["type"] == "service_unavailable"
    assert client.post("/v1/messages", json={}).status_code == 503
    # A key doesn't help while keys can't be checked.
    assert client.get(
        "/api/config", headers={"x-loom-gateway-key": "loom_whatever"}
    ).status_code == 503


def test_storage_unavailable_public_paths_and_health(gw_storage_down):
    client, gw = gw_storage_down
    assert client.get("/").status_code == 200
    assert client.get("/api/models").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "degraded"
    assert body["storage"] == "unavailable"
    assert body["auth_enabled"] is True


def test_key_lookup_error_fails_closed(gw_client, monkeypatch):
    client, gw, storage = gw_client

    def boom():
        raise RuntimeError("connection lost")

    monkeypatch.setattr(storage, "list_gateway_keys", boom)
    gw._gateway_keys_exist = None
    assert client.get("/api/config").status_code == 503
    assert client.get("/health").json()["status"] == "degraded"


def test_key_validation_error_fails_closed(gw_client, monkeypatch):
    client, gw, storage = gw_client
    storage.create_gateway_key("test")
    gw._gateway_keys_exist = None

    def boom(raw):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(storage, "validate_gateway_key", boom)
    resp = client.get("/api/config", headers={"x-loom-gateway-key": "loom_x"})
    assert resp.status_code == 503


def test_healthy_storage_reports_ok(gw_client):
    client, gw, storage = gw_client
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["storage"] == "ok"


async def test_storage_reconnect_backoff_then_success(tmp_path):
    from loom.gateway.app import GatewayState, _storage_reconnect

    gw = GatewayState()
    gw.storage_unavailable = True
    gw._gateway_keys_exist = False
    attempts = {"n": 0}
    delays = []

    def factory(cfg):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("db not up yet")
        return LoomStorage(str(tmp_path / "reconnect.db"))

    async def fake_sleep(d):
        delays.append(d)

    await _storage_reconnect(gw, factory=factory, initial_delay=2.0,
                             max_delay=5.0, sleep=fake_sleep)
    try:
        assert attempts["n"] == 3
        assert delays == [2.0, 4.0, 5.0]
        assert gw.storage is not None
        assert gw.storage_unavailable is False
        assert gw.storage_error is None
        assert gw._gateway_keys_exist is None or gw._gateway_keys_exist is False
    finally:
        gw.storage.close()


def test_attach_storage_failure_marks_unavailable():
    from loom.gateway.app import GatewayState, _attach_storage

    gw = GatewayState()

    def factory(cfg):
        raise ConnectionError("refused")

    assert _attach_storage(gw, factory) is False
    assert gw.storage is None
    assert gw.storage_unavailable is True
    assert gw.storage_error == "ConnectionError"
