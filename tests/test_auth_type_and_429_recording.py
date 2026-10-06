"""Auth-type label on rate-limit rows + 429 recording (AIProjects-6aeb follow-up)."""

import json

import httpx
import pytest

from loom.gateway.app import GatewayState, _record_request, _record_upstream_429
from loom.gateway.providers import anthropic as anthropic_module
from loom.gateway.providers.anthropic import AnthropicBackend
from loom.gateway.providers.base import ProviderError
from loom.observability import AuditLogger
from loom.observability.ratelimit_sink import AUTH_TYPE_RAW_KEY, RateLimitSink, build_row

OAUTH_KEY = "sk-ant-oat01-SECRETVALUE"
API_KEY = "sk-ant-api03-SECRETVALUE"
H429 = {
    "retry-after": "2",
    "request-id": "req_429",
    "anthropic-ratelimit-unified-status": "rejected",
    "anthropic-ratelimit-unified-5h-utilization": "0.99",
    "anthropic-ratelimit-requests-limit": "50",
    "anthropic-ratelimit-requests-remaining": "0",
}


def _backend(handler):
    b = AnthropicBackend(api_base="https://api.anthropic.com")
    b._client = httpx.AsyncClient(base_url=b.api_base, transport=httpx.MockTransport(handler))
    return b


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _s(_):
        return None

    monkeypatch.setattr(anthropic_module.asyncio, "sleep", _s)


class _SpySink:
    def __init__(self, raises=False):
        self.calls, self.raises = [], raises

    def emit(self, **kw):
        if self.raises:
            raise RuntimeError("boom")
        self.calls.append(kw)


def _state(tmp_path, sink=None):
    st = GatewayState()
    st.audit = AuditLogger(str(tmp_path / "audit.jsonl"), str(tmp_path / "metrics.jsonl"))
    st.ratelimit_sink = sink
    return st


def _audit(tmp_path):
    return [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]


def _ok_stream(request):
    async def _b():
        yield b"data: {}\n\n"

    return httpx.Response(200, headers={"request-id": "r"}, content=_b())


# ---------------------------------------------------------------- auth type
@pytest.mark.asyncio
@pytest.mark.parametrize("key,expected", [(OAUTH_KEY, "oauth"), (API_KEY, "api_key")])
async def test_snapshot_carries_auth_type_non_stream(key, expected):
    b = _backend(lambda r: httpx.Response(200, headers={"request-id": "r"}, json={}))
    await b._complete({"model": "m"}, api_key=key)
    assert b._last_ratelimit["auth_type"] == expected
    assert key not in json.dumps(b._last_ratelimit)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,expected", [(OAUTH_KEY, "oauth"), (API_KEY, "api_key")])
async def test_snapshot_carries_auth_type_stream(key, expected):
    b = _backend(_ok_stream)
    _ = [c async for c in b._stream({"model": "m"}, api_key=key)]
    assert b._last_ratelimit["auth_type"] == expected
    assert key not in json.dumps(b._last_ratelimit)


def test_record_request_labels_audit_and_sink(tmp_path):
    sink = _SpySink()
    st = _state(tmp_path, sink)
    rl = {"ratelimit_requests_limit": 5, "upstream_request_id": "r1", "auth_type": "oauth"}
    _record_request(
        st, request_id="r1", method="POST", path="/v1/messages", source="cc",
        provider="anthropic", model="m", requested_model=None, task_type="t",
        routing_reason="x", status_code=200, latency_ms=1.0, ratelimit=rl,
    )
    rec = _audit(tmp_path)[0]
    assert rec["auth_type"] == "oauth"
    assert "auth_type" not in rec.get("ratelimit", {})
    assert sink.calls[0]["auth_type"] == "oauth"


def test_auth_type_alone_does_not_trigger_sink(tmp_path):
    sink = _SpySink()
    st = _state(tmp_path, sink)
    _record_request(
        st, request_id="r", method="POST", path="/p", source="s", provider="anthropic",
        model="m", requested_model=None, task_type="t", routing_reason="x",
        status_code=200, latency_ms=1.0, ratelimit={"auth_type": "oauth"},
    )
    assert sink.calls == []


def test_build_row_auth_type_in_raw_headers_and_optional_column():
    row = build_row(source="s", method="POST", path="/p", status_code=200, model="m",
                    ratelimit={"auth_type": "oauth", "raw_headers": {"request-id": "x"}})
    assert json.loads(row["raw_headers"])[AUTH_TYPE_RAW_KEY] == "oauth"
    assert "auth_type" not in row  # default: no new column
    row = build_row(source="s", method="POST", path="/p", status_code=200, model="m",
                    ratelimit={"auth_type": "api_key"}, auth_type_column=True)
    assert row["auth_type"] == "api_key"
    assert json.loads(row["raw_headers"])[AUTH_TYPE_RAW_KEY] == "api_key"
    row = build_row(source="s", method="POST", path="/p", status_code=200, model="m",
                    ratelimit={"auth_type": "junk"}, auth_type_column=True)
    assert row["auth_type"] is None and row["raw_headers"] is None


def test_sink_sql_includes_column_only_when_enabled():
    off = RateLimitSink("d", start=False)
    on = RateLimitSink("d", start=False, auth_type_column=True)
    assert "auth_type" not in off._sql
    assert on._sql.count("auth_type") == 2


# ---------------------------------------------------------------- 429 recording
@pytest.mark.asyncio
async def test_retried_429_calls_hook_once_non_stream():
    n = {"n": 0}

    def handler(request):
        n["n"] += 1
        if n["n"] == 1:
            return httpx.Response(429, headers=H429, json={"error": "rl"})
        return httpx.Response(200, json={"ok": 1})

    seen = []
    b = _backend(handler)
    assert await b._complete({"model": "m"}, OAUTH_KEY, on_429=seen.append) == {"ok": 1}
    assert n["n"] == 2 and len(seen) == 1
    assert seen[0]["retry_after"] == "2"
    assert seen[0]["auth_type"] == "oauth"
    assert seen[0]["ratelimit_unified_status"] == "rejected"


@pytest.mark.asyncio
async def test_retried_429_calls_hook_once_stream():
    n = {"n": 0}

    def handler(request):
        n["n"] += 1
        if n["n"] == 1:
            return httpx.Response(429, headers=H429, json={"error": "rl"})
        return _ok_stream(request)

    seen = []
    b = _backend(handler)
    out = [c async for c in b._stream({"model": "m"}, OAUTH_KEY, on_429=seen.append)]
    assert out == [b"data: {}\n\n"]
    assert n["n"] == 2 and len(seen) == 1
    assert seen[0]["auth_type"] == "oauth" and seen[0]["retry_after"] == "2"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_terminal_429_error_carries_snapshot_and_same_client_error(stream):
    hook_calls = []
    b = _backend(lambda r: httpx.Response(429, headers={"request-id": "r"}, json={"error": "rl"}))
    with pytest.raises(ProviderError) as ei:
        if stream:
            async for _ in b._stream({"model": "m"}, API_KEY, on_429=hook_calls.append):
                pass
        else:
            await b._complete({"model": "m"}, API_KEY, on_429=hook_calls.append)
    assert ei.value.status_code == 429
    assert ei.value.payload == {"error": "rl"}  # client-visible body unchanged
    assert ei.value.ratelimit["auth_type"] == "api_key"
    assert ei.value.ratelimit["upstream_request_id"] == "r"
    assert hook_calls == []  # terminal 429 is recorded by the caller, not the hook


@pytest.mark.asyncio
async def test_hook_exception_never_breaks_retry():
    n = {"n": 0}

    def handler(request):
        n["n"] += 1
        if n["n"] == 1:
            return httpx.Response(429, headers=H429, json={})
        return httpx.Response(200, json={"ok": 1})

    def bad(_):
        raise RuntimeError("x")

    b = _backend(handler)
    assert await b._complete({"model": "m"}, OAUTH_KEY, on_429=bad) == {"ok": 1}


def _snap_429():
    return anthropic_module._snapshot(
        httpx.Response(429, headers=H429), {"Authorization": "Bearer x"},
    )


def test_record_upstream_429_writes_audit_and_sink(tmp_path):
    sink = _SpySink()
    st = _state(tmp_path, sink)
    _record_upstream_429(
        st, request_id="r9", path="/v1/messages", source="cc", provider="anthropic",
        model="m", requested_model="rm", ratelimit=_snap_429(), latency_ms=12.0,
        client_app="cli",
    )
    rec = _audit(tmp_path)[0]
    assert rec["status_code"] == 429 and rec["auth_type"] == "oauth"
    assert rec["upstream_request_id"] == "req_429"
    assert rec["ratelimit"]["ratelimit_unified_status"] == "rejected"
    call = sink.calls[0]
    assert call["status_code"] == 429 and call["auth_type"] == "oauth"
    row = build_row(source="s", **call)
    assert row["status_code"] == 429
    assert row["retry_after"] == 2
    assert row["rl_unified_status"] == "rejected"
    assert row["rl_unified_5h_util"] == 0.99
    assert json.loads(row["raw_headers"])[AUTH_TYPE_RAW_KEY] == "oauth"
    assert not (tmp_path / "metrics.jsonl").exists()  # no metrics row for a 429 attempt


def test_record_upstream_429_never_raises(tmp_path):
    st = _state(tmp_path, _SpySink(raises=True))
    _record_upstream_429(st, request_id="r", path="/p", source="s", provider="anthropic",
                         model="m", requested_model=None, ratelimit=_snap_429(), latency_ms=1.0)
    _record_upstream_429(GatewayState(), request_id="r", path="/p", source="s",
                         provider="anthropic", model="m", requested_model=None,
                         ratelimit=None, latency_ms=1.0)

    class _BadAudit:
        def log_request(self, **kw):
            raise RuntimeError("x")

    st.audit = _BadAudit()
    _record_upstream_429(st, request_id="r", path="/p", source="s", provider="anthropic",
                         model="m", requested_model=None, ratelimit=_snap_429(), latency_ms=1.0)


def test_non_anthropic_provider_not_sunk(tmp_path):
    sink = _SpySink()
    st = _state(tmp_path, sink)
    _record_upstream_429(st, request_id="r", path="/p", source="s", provider="openai",
                         model="m", requested_model=None, ratelimit=_snap_429(), latency_ms=1.0)
    assert sink.calls == [] and _audit(tmp_path)[0]["status_code"] == 429


def test_audit_log_never_contains_credentials(tmp_path):
    st = _state(tmp_path, _SpySink())
    _record_upstream_429(st, request_id="r", path="/p", source="s", provider="anthropic",
                         model="m", requested_model=None, ratelimit=_snap_429(), latency_ms=1.0)
    text = (tmp_path / "audit.jsonl").read_text()
    assert "SECRETVALUE" not in text and "Bearer" not in text
