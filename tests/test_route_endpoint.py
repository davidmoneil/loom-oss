"""Tests for the side-effect-free `/v1/route` endpoint (Fix D, 2026-09-26
routing decision): returns a routing decision without executing a
completion or contacting a provider, so callers like Nexus's executor.sh can
ask "what would this route to" without paying for a real request.
"""
import pytest
from fastapi.testclient import TestClient

from loom.config import LoomConfig, ModelConfig, ProviderConfig, SourcePolicy


def _routable_config():
    """A minimal config with a real provider/model, so `_select_model`'s
    config-fallback path (routing engine unset in the bare test app) has
    something to resolve — the default `GatewayState.config` has none."""
    return LoomConfig(
        providers=[
            ProviderConfig(
                name="anthropic",
                api_base="https://api.anthropic.com",
                models=[
                    ModelConfig(model_id="sonnet", tier="standard",
                                cost_per_1k_input=0.01, cost_per_1k_output=0.01,
                                supports_tools=True),
                ],
            ),
        ],
        sources={"default": SourcePolicy()},
    )


@pytest.fixture
def routable_gateway():
    from loom.gateway.app import app

    gw = app.state.gateway
    original_config = gw.config
    gw.config = _routable_config()
    try:
        yield app
    finally:
        gw.config = original_config


def test_route_returns_a_model_decision(routable_gateway):
    client = TestClient(routable_gateway)
    resp = client.post(
        "/v1/route",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "model" in data and data["model"]
    assert "task_type" in data
    assert "routing_reason" in data
    assert data["source"] == "default"


def test_route_honors_preferred_model_field(routable_gateway):
    client = TestClient(routable_gateway)
    # A garbage preferred_model must never error the endpoint — it just
    # fails eligibility and falls through to normal routing (same contract
    # as _select_model / RoutingEngine.recommend tested elsewhere).
    resp = client.post(
        "/v1/route",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "preferred_model": "not-a-real-model",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "model" in data and data["model"]


def test_route_rejects_invalid_json():
    from loom.gateway.app import app

    client = TestClient(app)
    resp = client.post(
        "/v1/route",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_route_never_touches_provider_backends(routable_gateway, monkeypatch):
    """A /v1/route call must never invoke a provider backend — it only
    resolves a model name, it does not execute anything."""
    from loom.gateway import app as app_module

    called = {"hit": False}

    def _boom(*args, **kwargs):
        called["hit"] = True
        raise AssertionError("provider backend should never be called by /v1/route")

    monkeypatch.setattr(app_module.GatewayState, "resolve_provider", _boom, raising=False)

    client = TestClient(routable_gateway)
    resp = client.post(
        "/v1/route",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    assert called["hit"] is False
