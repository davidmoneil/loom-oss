"""Request tags, header discovery, and rate limits split by credential type."""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from loom.config import RequestTagsConfig
from loom.gateway.app import GatewayState, _record_request, app
from loom.observability import AuditLogger
from loom.observability.request_tags import (
    HeaderObserver,
    build_header_map,
    current_tags,
    extract_tags,
    is_denied_header,
    sanitize_value,
    tag_like_pattern,
)
from loom.storage.sqlite import LoomStorage


def _cfg(**kw):
    base = dict(enabled=True, headers={"x-loom-job": "job", "x-loom-project": "project"})
    base.update(kw)
    return RequestTagsConfig(**base)


# ------------------------------------------------------------------ extraction
def test_disabled_by_default_extracts_nothing():
    cfg = RequestTagsConfig(headers={"x-loom-job": "job"})
    assert cfg.enabled is False
    assert extract_tags(Headers({"x-loom-job": "a"}), cfg) == {}


def test_extracts_configured_headers_only():
    h = Headers({"X-Loom-Job": "nightly", "x-other": "zzz", "x-loom-project": "p1"})
    assert extract_tags(h, _cfg()) == {"job": "nightly", "project": "p1"}


@pytest.mark.parametrize("name", [
    "authorization", "Authorization", "x-api-key", "cookie", "proxy-authorization",
    "x-loom-gateway-key", "x-session-token", "x-client-secret", "x-oauth-thing",
    "x-my-key",
])
def test_credential_headers_denied(name):
    assert is_denied_header(name)
    assert build_header_map({name: "leak"}) == {}
    cfg = _cfg(headers={name: "leak"})
    assert extract_tags(Headers({name: "sk-ant-secret"}), cfg) == {}


def test_values_sanitized_and_limited():
    assert sanitize_value("a\r\nb\x00c\x1b[31m", 50) == "a b c [31m"
    cfg = _cfg(max_value_length=5)
    assert extract_tags(Headers({"x-loom-job": "abcdefghij"}), cfg) == {"job": "abcde"}


def test_max_tags_and_invalid_tag_names():
    cfg = _cfg(headers={"x-a": "a", "x-b": "b", "x-c": "c", "x-d": "Bad Name!"}, max_tags=2)
    out = extract_tags(Headers({"x-a": "1", "x-b": "2", "x-c": "3", "x-d": "4"}), cfg)
    assert len(out) == 2 and "Bad Name!" not in out


# ------------------------------------------------------------------ discovery
def test_header_observer_names_only_and_skips_denied():
    obs = HeaderObserver()
    obs.observe(Headers({"x-loom-job": "SECRETVALUE", "authorization": "Bearer t",
                         "x-api-key": "k", "user-agent": "ua"}), now=7200)
    obs.observe(Headers({"x-loom-job": "again"}), now=7300)
    snap = {r["name"]: r["count"] for r in obs.snapshot(24, now=7300)}
    assert snap == {"x-loom-job": 2, "user-agent": 1}
    assert "SECRETVALUE" not in json.dumps(obs.snapshot(24, now=7300))


def test_header_observer_window():
    obs = HeaderObserver()
    obs.observe(Headers({"x-old": "1"}), now=0)
    obs.observe(Headers({"x-new": "1"}), now=3600 * 30)
    assert [r["name"] for r in obs.snapshot(2, now=3600 * 30)] == ["x-new"]


# ------------------------------------------------------------------ storage
@pytest.fixture
def store(tmp_path):
    s = LoomStorage(str(tmp_path / "t.db"))
    s.connect()
    yield s
    s.close()


def _metric(store, rid, tags=None, cost=0.01):
    store.record_metrics(
        request_id=rid, model="m", provider="anthropic", tokens_in=10, tokens_out=5,
        latency_ms=1.0, cost=cost, compressed=False, compression_ratio=1.0,
        requested_model="m", task_type="t", message_count=1, source="s",
        tags=json.dumps(tags, separators=(",", ":"), sort_keys=True) if tags else None,
    )


def test_tags_roundtrip_filter_and_group(store):
    _metric(store, "r1", {"job": "nightly", "project": "a"}, cost=0.5)
    _metric(store, "r2", {"job": "nightly"}, cost=0.25)
    _metric(store, "r3", {"job": "weekly"})
    _metric(store, "r4")
    page = store.get_audit_entries(limit=10, tag="job:nightly")
    assert {e["request_id"] for e in page["entries"]} == {"r1", "r2"}
    assert page["entries"][0]["tags"]["job"] == "nightly"
    assert store.get_audit_entries(limit=10, tag="job:night")["total"] == 0  # exact value
    assert store.get_audit_entries(limit=10, tag="bogus")["total"] == 4  # malformed: ignored
    untagged = [e for e in store.get_audit_entries(limit=10)["entries"] if e["request_id"] == "r4"]
    assert untagged[0]["tags"] == {}
    ts = store.get_metrics_timeseries(1, 3600)
    assert ts["by_tag"]["job"]["nightly"]["requests"] == 2
    assert ts["by_tag"]["job"]["nightly"]["cost"] == 0.75
    assert ts["by_tag"]["project"]["a"]["requests"] == 1


def test_tag_filter_escapes_like_wildcards(store):
    _metric(store, "r1", {"job": "a_b"})
    _metric(store, "r2", {"job": "axb"})
    assert {e["request_id"] for e in store.get_audit_entries(limit=5, tag="job:a_b")["entries"]} == {"r1"}
    assert "!" in tag_like_pattern("job", "a_b")


def test_migration_adds_columns_to_old_db(tmp_path):
    import sqlite3

    s = LoomStorage(str(tmp_path / "x.db"))
    s.connect()
    s.close()
    db = sqlite3.connect(str(tmp_path / "x.db"))
    cols = {r[1] for r in db.execute("PRAGMA table_info(metrics)")}
    rl = {r[1] for r in db.execute("PRAGMA table_info(rate_limits)")}
    db.close()
    assert "tags" in cols
    assert {"auth_type", "util_5h", "util_7d"} <= rl


# ------------------------------------------------------------------ record_request
def test_record_request_stores_tags_from_context(tmp_path, store):
    st = GatewayState()
    st.storage = store
    st.audit = AuditLogger(str(tmp_path / "a.jsonl"), str(tmp_path / "m.jsonl"))
    tok = current_tags.set({"job": "nightly"})
    try:
        _record_request(
            st, request_id="rq", method="POST", path="/v1/messages", source="s",
            provider="anthropic", model="m", requested_model="m", task_type="t",
            routing_reason="r", status_code=200, latency_ms=1.0, client_job="legacy-job",
        )
    finally:
        current_tags.reset(tok)
    rec = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
    assert rec["tags"] == {"job": "nightly"}
    assert rec["client_job"] == "legacy-job"  # existing audit key unchanged
    assert store.get_audit_entries(tag="job:nightly")["total"] == 1


# ------------------------------------------------------------------ rate limits by credential
def _rl(store, auth, **extra):
    store.record_rate_limits(
        request_id="x", provider="anthropic", model="m", auth_type=auth,
        ratelimit=extra,
    )


def test_rate_limits_split_by_auth_type(store):
    _rl(store, "oauth", ratelimit_unified_5h_utilization=0.4,
        ratelimit_unified_7d_utilization=0.2, ratelimit_unified_5h_status="allowed")
    _rl(store, "api_key", ratelimit_requests_limit=50, ratelimit_requests_remaining=10)
    o = store.get_rate_limit_current("anthropic", "oauth")
    k = store.get_rate_limit_current("anthropic", "api_key")
    assert o["util_5h"] == 0.4 and o["util_7d"] == 0.2 and o["status_5h"] == "allowed"
    assert k["requests_limit"] == 50 and k["util_5h"] is None
    assert store.get_rate_limit_current("anthropic", "nope") is None
    assert store.get_rate_limit_trend(1, "anthropic", "oauth")[0]["avg_5h"] == 0.4


# ------------------------------------------------------------------ API + middleware
def test_api_request_tags_and_rate_limits_shape(tmp_path):
    with TestClient(app) as c:
        gw = app.state.gateway
        orig = gw.config.request_tags
        gw.config.request_tags = _cfg()
        try:
            c.get("/health", headers={"x-loom-job": "j", "authorization": "Bearer SECRETVALUE"})
            data = c.get("/api/request-tags").json()
        finally:
            gw.config.request_tags = orig
        assert data["enabled"] is True
        names = {r["name"] for r in data["seen_headers"]}
        assert "x-loom-job" in names and "authorization" not in names
        assert "SECRETVALUE" not in json.dumps(data)
        assert {"header": "x-loom-job", "tag": "job"} in data["configured"]
        rl = c.get("/api/rate-limits").json()
        assert set(rl["by_auth_type"]) == {"oauth", "api_key"}


def test_api_request_tags_disabled_lists_nothing():
    with TestClient(app) as c:
        data = c.get("/api/request-tags").json()
        assert data["enabled"] is False and data["seen_headers"] == []
