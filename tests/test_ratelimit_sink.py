"""Optional rate-limit sink + audit join fields (AIProjects-6aeb)."""

import json
import logging
import threading
import time

import httpx

from loom.config import LoomConfig, RateLimitSinkConfig
from loom.gateway.app import (
    GatewayState,
    _client_identity,
    _record_request,
    _stream_stop_reason,
)
from loom.gateway.providers.anthropic import (
    SINK_ONLY_KEYS,
    _extract_ratelimit_headers,
)
from loom.observability import AuditLogger
from loom.observability.ratelimit_sink import COLUMNS, RateLimitSink, build_row, build_sink

HEADERS = {
    "request-id": "req_011abc",
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-reset": "1790000000",
    "anthropic-ratelimit-unified-5h-utilization": "0.42",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.61",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d_sonnet-utilization": "0.33",
    "anthropic-ratelimit-requests-limit": "1000",
    "anthropic-ratelimit-requests-remaining": "999",
    "anthropic-ratelimit-requests-reset": "2026-10-06T12:00:00Z",
    "authorization": "Bearer should-never-appear",
    "content-type": "application/json",
}


def _snapshot():
    return _extract_ratelimit_headers(httpx.Response(200, headers=HEADERS))


# ------------------------------------------------------------------ config
def test_disabled_by_default():
    cfg = LoomConfig()
    assert cfg.observability.ratelimit_sink.enabled is False
    assert build_sink(cfg.observability.ratelimit_sink) is None
    assert build_sink(None) is None


def test_enabled_without_dsn_env_is_off(monkeypatch):
    monkeypatch.delenv("X_TEST_DSN", raising=False)
    assert build_sink(RateLimitSinkConfig(enabled=True, dsn_env="X_TEST_DSN")) is None


def test_bad_table_name_rejected_not_raised(monkeypatch):
    monkeypatch.setenv("X_TEST_DSN", "postgresql://nowhere/x")
    cfg = RateLimitSinkConfig(enabled=True, dsn_env="X_TEST_DSN", table="t; drop table x")
    assert build_sink(cfg) is None


# ------------------------------------------------------------------ extraction
def test_extract_adds_request_id_raw_headers_and_7d_model():
    rl = _snapshot()
    assert rl["upstream_request_id"] == "req_011abc"
    assert rl["ratelimit_unified_7d_model_name"] == "sonnet"
    assert rl["ratelimit_unified_7d_model_utilization"] == 0.33
    assert "authorization" not in rl["raw_headers"]
    assert "content-type" not in rl["raw_headers"]
    assert rl["raw_headers"]["anthropic-ratelimit-unified-5h-utilization"] == "0.42"
    assert rl["ratelimit_unified_5h_utilization"] == 0.42


# ------------------------------------------------------------------ rows
def test_build_row_maps_every_column_and_no_content():
    row = build_row(
        source="loom-oss", method="POST", path="/v1/messages", status_code=200,
        model="claude-sonnet-4-5", ratelimit=_snapshot(), tokens_in=10, tokens_out=5,
        cache_creation_tokens=2, cache_read_tokens=3, cost_usd=0.0123456789,
        latency_ms=812.4, stop_reason="end_turn",
    )
    assert set(row) == set(COLUMNS)
    assert row["request_id"] == "req_011abc"
    assert row["rl_unified_5h_util"] == 0.42
    assert row["rl_unified_7d_model_name"] == "sonnet"
    assert row["rl_requests_limit"] == 1000
    assert row["rl_requests_reset"] == "2026-10-06T12:00:00Z"
    assert row["rl_unified_reset"] == 1790000000
    assert row["response_ms"] == 812
    assert row["stop_reason"] == "end_turn"
    blob = json.dumps(row)
    assert "should-never-appear" not in blob
    assert json.loads(row["raw_headers"])["request-id"] == "req_011abc"


# ------------------------------------------------------------------ sink behaviour
class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params):
        self.conn.rows.append((sql, params))

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.rows = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _emit(sink, **kw):
    sink.emit(method="POST", path="/v1/messages", status_code=200, model="m",
              ratelimit=_snapshot(), **kw)


def test_sink_writes_row_in_background():
    conn = _FakeConn()
    sink = RateLimitSink("dsn", connect=lambda dsn: conn)
    try:
        _emit(sink, tokens_in=1, tokens_out=2)
        assert _wait(lambda: len(conn.rows) == 1)
        sql, params = conn.rows[0]
        assert sql.startswith("INSERT INTO api_headers (")
        assert params["request_id"] == "req_011abc"
    finally:
        sink.close()


def test_emit_never_blocks_when_db_is_hung(caplog):
    gate = threading.Event()

    def hung_connect(dsn):
        gate.wait(10)
        raise RuntimeError("down")

    sink = RateLimitSink("dsn", queue_size=3, connect=hung_connect)
    try:
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="loom.ratelimit_sink"):
            for _ in range(50):
                _emit(sink)
        assert time.monotonic() - t0 < 1.0
        assert sink.dropped > 0
        assert len([r for r in caplog.records if "queue full" in r.getMessage()]) <= 1
    finally:
        gate.set()
        sink.close()


def test_fail_open_when_db_down_warns_once_per_minute(caplog):
    attempts = []

    def bad_connect(dsn):
        attempts.append(1)
        raise ConnectionError("secret-host refused")

    sink = RateLimitSink("dsn", connect=bad_connect, start=False)
    with caplog.at_level(logging.WARNING, logger="loom.ratelimit_sink"):
        for _ in range(5):
            assert sink.flush_one({c: None for c in COLUMNS}) is False
    warns = [r for r in caplog.records if "write failed" in r.getMessage()]
    assert len(attempts) == 5 and len(warns) == 1
    assert "secret-host" not in warns[0].getMessage()

    # recovers once the database is back
    conn = _FakeConn()
    sink._connect = lambda dsn: conn
    assert sink.flush_one({c: None for c in COLUMNS}) is True
    assert len(conn.rows) == 1


# ------------------------------------------------------------------ gateway wiring
class _SpySink:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def emit(self, **kw):
        if self.raises:
            raise RuntimeError("boom")
        self.calls.append(kw)


def _record(state, **over):
    kw = dict(
        request_id="r1", method="POST", path="/v1/messages", source="cc",
        provider="anthropic", model="claude-sonnet-4-5", requested_model=None,
        task_type="chat", routing_reason="x", status_code=200, latency_ms=10.0,
        usage={"input_tokens": 100, "output_tokens": 20,
               "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10},
        cost=0.5, ratelimit=_snapshot(), client_app="cli", client_job="job-1",
        stop_reason="end_turn",
    )
    kw.update(over)
    _record_request(state, **kw)


def test_record_request_emits_sink_row_and_strips_extras_from_audit(tmp_path):
    state = GatewayState()
    state.audit = AuditLogger(str(tmp_path / "audit.jsonl"), str(tmp_path / "metrics.jsonl"))
    state.ratelimit_sink = _SpySink()
    _record(state)

    assert len(state.ratelimit_sink.calls) == 1
    call = state.ratelimit_sink.calls[0]
    assert call["stop_reason"] == "end_turn"
    assert call["tokens_in"] == 100  # raw split, cache excluded
    assert call["cache_read_tokens"] == 30
    assert call["ratelimit"]["upstream_request_id"] == "req_011abc"

    rec = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert rec["upstream_request_id"] == "req_011abc"
    assert rec["client_app"] == "cli" and rec["client_job"] == "job-1"
    assert not (set(rec["ratelimit"]) & SINK_ONLY_KEYS)


def test_record_request_survives_sink_exception_and_skips_non_anthropic():
    state = GatewayState()
    state.ratelimit_sink = _SpySink(raises=True)
    _record(state)  # must not raise
    state.ratelimit_sink = _SpySink()
    _record(state, provider="ollama")
    assert state.ratelimit_sink.calls == []


def test_record_request_without_sink_is_unchanged(tmp_path):
    state = GatewayState()
    state.audit = AuditLogger(str(tmp_path / "audit.jsonl"), str(tmp_path / "metrics.jsonl"))
    assert state.ratelimit_sink is None
    _record(state)
    rec = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert rec["status_code"] == 200


# ------------------------------------------------------------------ helpers
def test_client_identity():
    assert _client_identity({"x-app": "cli", "x-nexus-job": "nightly"}) == ("cli", "nightly")
    assert _client_identity({"user-agent": "claude-cli/2.1 (external)"}) == ("claude-cli", "")
    assert _client_identity({"user-agent": "weird-agent/1"}) == ("unknown", "")
    assert _client_identity({}) == ("", "")


def test_stream_stop_reason():
    lines = [
        'data: {"type":"message_start","message":{"stop_reason":null}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        "data: [DONE]",
    ]
    assert _stream_stop_reason(lines) == "tool_use"
    assert _stream_stop_reason(["data: {}"]) is None
