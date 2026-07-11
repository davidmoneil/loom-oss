"""Contract tests for the observability API (docs/observability-api.md)."""

from fastapi.testclient import TestClient


def _client():
    from loom.gateway.app import app

    return TestClient(app)


def test_health_contract_fields():
    resp = _client().get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "degraded")
    assert data["uptime_seconds"] >= 0
    assert "requests" in data and "errors" in data
    comp = data["compression"]
    for key in ("enabled", "default_tier", "tokens_before", "tokens_after",
                "tokens_saved", "compression_ratio"):
        assert key in comp
    sess = data["sessions"]
    assert set(sess) >= {"supported", "sessions", "total_turns"}


def test_costs_contract_shape():
    resp = _client().get("/api/costs?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert data["window_days"] == 7
    totals = data["totals"]
    for key in ("requests", "tokens_in", "tokens_out", "cost_usd",
                "tokens_saved", "savings_usd"):
        assert key in totals
    for section in ("by_model", "by_source", "by_tier", "by_day", "by_hour"):
        assert isinstance(data[section], list)


def test_sessions_contract_shape():
    resp = _client().get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["supported"], bool)
    assert isinstance(data["sessions"], int)
    assert isinstance(data["entries"], list)


def test_audit_contract_shape():
    resp = _client().get("/api/audit?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) >= {"total", "offset", "limit", "entries"}


def test_cost_summary_with_data(tmp_path):
    """End-to-end: rows recorded through storage appear in /api/costs buckets."""
    from loom.storage import LoomStorage

    store = LoomStorage(db_path=str(tmp_path / "costs.db"))
    store.connect()
    store.record_metrics(
        request_id="r1",
        model="haiku",
        provider="anthropic",
        tokens_in=1000,
        tokens_out=100,
        latency_ms=50.0,
        cost=0.005,
        compressed=True,
        compression_ratio=0.5,
        source="pytest",
        tokens_saved=1000,
    )
    summary = store.get_cost_summary(days=1)
    store.close()

    assert summary["totals"]["requests"] == 1
    assert summary["totals"]["tokens_in"] == 1000
    assert summary["totals"]["cost_usd"] == 0.005
    # Measured savings recorded at compression time, not derived from ratio.
    assert summary["totals"]["tokens_saved"] == 1000
    assert summary["by_model"][0]["model"] == "haiku"
    assert summary["by_source"][0]["source"] == "pytest"
    assert len(summary["by_day"]) == 1
    assert len(summary["by_hour"]) == 1
