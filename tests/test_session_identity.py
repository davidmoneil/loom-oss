"""Client-provided session ids + per-session stats (AIProjects-juc9)."""

import json
import uuid

from fastapi.testclient import TestClient

from loom.gateway.app import _extract_session_signals

MSGS = [{"role": "user", "content": "hello"}]
CC_USER_ID = json.dumps({"device_id": "d" * 64, "account_uuid": "acct",
                         "session_id": "171c1a07-326d-4e45-8d6f-5497e16f05f0"})


def test_claude_code_metadata_session_id_used():
    sig = _extract_session_signals(MSGS, "cc", {}, {"metadata": {"user_id": CC_USER_ID}})
    assert sig["session_id"] == "cc-171c1a07-326d-4e45-8d6f-5497e16f05f0"
    assert sig["client_session_id"] == "171c1a07-326d-4e45-8d6f-5497e16f05f0"


def test_explicit_header_wins():
    sig = _extract_session_signals(MSGS, "cc", {"x-loom-session-id": "job-run-42"},
                                   {"metadata": {"user_id": CC_USER_ID}})
    assert sig["session_id"] == "job-run-42"


def test_malformed_header_and_plain_user_id_fall_back_to_hash():
    sig = _extract_session_signals(MSGS, "api", {"x-loom-session-id": "bad id; drop"},
                                   {"metadata": {"user_id": "user-123"}})
    assert sig["session_id"].startswith("gw-")
    assert sig["client_session_id"] == ""


def _row(storage, sid, prompts, calls, heavy=0, saved=0, before=0):
    storage.record_metrics(
        request_id=uuid.uuid4().hex[:12], model="sonnet", provider="anthropic",
        tokens_in=100, tokens_out=10, latency_ms=1.0, cost=0.01, source="pytest",
        session_id=sid, compressed=saved > 0, tokens_saved=saved,
        tokens_before=before, tokens_after=before - saved,
        skip_reasons=json.dumps({"applied_heavy": heavy, "turn": {
            "user_prompts": prompts, "tool_calls": calls, "tool_calls_this_turn": calls}}),
    )


def test_session_detail_rollup(storage):
    _row(storage, "cc-x", 1, 2)
    _row(storage, "cc-x", 2, 5, heavy=2, saved=300, before=1000)
    _row(storage, "other", 1, 1)
    d = storage.get_session_detail("cc-x")
    assert d["requests"] == 2 and d["user_prompts"] == 2 and d["tool_calls"] == 5
    assert d["evictions"] == 2 and d["tokens_saved"] == 300 and d["saved_pct"] == 30.0
    assert storage.get_session_detail("missing") is None


def test_session_stats_endpoint_and_response_header(tmp_path):
    from loom.gateway.app import app
    from loom.storage.sqlite import LoomStorage

    gw = app.state.gateway
    prev_storage, prev_cache = gw.storage, getattr(gw, "_gateway_keys_exist", None)
    storage = LoomStorage(str(tmp_path / "s.db"))
    storage.connect()
    gw.storage, gw._gateway_keys_exist = storage, None
    try:
        _row(storage, "cc-abc", 3, 7)
        client = TestClient(app)
        r = client.get("/api/sessions/cc-abc/stats")
        assert r.status_code == 200 and r.json()["user_prompts"] == 3
        assert client.get("/api/sessions/nope/stats").status_code == 404
        assert client.get("/api/sessions/bad%20id/stats").status_code == 400
    finally:
        storage.close()
        gw.storage, gw._gateway_keys_exist = prev_storage, prev_cache
