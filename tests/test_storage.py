"""Storage backend tests — run against SQLite always, Postgres when opted in.

Set LOOM_TEST_POSTGRES_DSN to a *disposable* database to exercise the Postgres
backend (tables are created and rows written; never point this at production).
"""

import uuid

import pytest

from loom.storage import LoomStorage, create_storage

# The parametrized `storage` fixture (SQLite always, Postgres when
# LOOM_TEST_POSTGRES_DSN is set) comes from tests/conftest.py.


def _record_request(store, request_id, model="haiku", cost=0.01, **metrics_kwargs):
    store.record_routing_decision(
        request_id=request_id,
        source="pytest",
        task_type="test",
        model=model,
        reason="unit-test",
        model_recommended=model,
        determinism_score=0.5,
        alternatives=["sonnet"],
    )
    store.record_metrics(
        request_id=request_id,
        model=model,
        provider="anthropic",
        tokens_in=100,
        tokens_out=50,
        latency_ms=123.0,
        cost=cost,
        compressed=True,
        compression_ratio=0.6,
        requested_model=model,
        task_type="test",
        message_count=3,
        source="pytest",
        **metrics_kwargs,
    )


def test_routing_stats_roundtrip(storage):
    rid = f"req-{uuid.uuid4().hex[:12]}"
    _record_request(storage, rid, model="haiku", cost=0.02)
    stats = storage.get_routing_stats(hours=1)
    assert stats["total_decisions"] >= 1
    assert stats["by_model"].get("haiku", 0) >= 1
    assert stats["request_count"] >= 1
    assert stats["tokens_in"] >= 100
    assert stats["total_cost"] >= 0.02


def test_audit_entries_and_filters(storage):
    rid = f"req-{uuid.uuid4().hex[:12]}"
    _record_request(storage, rid, model="sonnet")
    page = storage.get_audit_entries(limit=10, search=rid)
    assert page["total"] >= 1
    assert any(e["request_id"] == rid for e in page["entries"])

    filtered = storage.get_audit_entries(limit=10, model="no-such-model", search=rid)
    assert filtered["total"] == 0


def test_metrics_timeseries_shape(storage):
    rid = f"req-{uuid.uuid4().hex[:12]}"
    _record_request(storage, rid)
    series = storage.get_metrics_timeseries(hours=1, bucket_seconds=60)
    assert isinstance(series, dict)


def test_compression_cache_roundtrip(storage):
    chash = f"hash-{uuid.uuid4().hex[:12]}"
    storage.put_compression_cached(
        content_hash=chash,
        age_ratio=0.5,
        compressed="short text",
        tier="medium",
        tokens_before=100,
        tokens_after=40,
    )
    hit = storage.get_compression_cached(chash, age_ratio=0.55)
    assert hit is not None
    assert hit["compressed_text"] == "short text"
    assert hit["tier"] == "medium"
    assert storage.get_compression_cached(f"missing-{chash}", age_ratio=0.5) is None


def test_content_importance_scoring(storage):
    chash = f"imp-{uuid.uuid4().hex[:12]}"
    storage.record_content_importance(chash)
    storage.record_content_importance(chash)
    scores = storage.get_content_importance([chash])
    assert scores[chash] == pytest.approx(0.75)
    assert storage.get_content_importance([]) == {}


def test_session_tracking(storage):
    sid = f"sess-{uuid.uuid4().hex[:12]}"
    assert storage.touch_session(sid, source="pytest") == 1
    assert storage.touch_session(sid, source="pytest", tokens=100, cost=0.01) == 2

    stats = storage.get_session_stats()
    assert stats["sessions"] >= 1
    assert stats["total_turns"] >= 2

    entries = storage.list_sessions(hours=1)
    mine = next(e for e in entries if e["session_id"] == sid)
    assert mine["source"] == "pytest"
    assert mine["turns"] == 2
    assert mine["last_seen"] > 0


def test_session_metadata_columns(storage):
    """touch_session stores and retrieves client metadata (schema v6)."""
    sid = f"sess-meta-{uuid.uuid4().hex[:12]}"
    storage.touch_session(
        sid, source="pytest",
        client_type="claude-code",
        user_id="test@example.com",
        api_key_suffix="abcd1234",
        system_hash="abc123def456",
    )
    entries = storage.list_sessions(hours=1)
    mine = next(e for e in entries if e["session_id"] == sid)
    assert mine["client_type"] == "claude-code"
    assert mine["user_id"] == "test@example.com"
    assert mine["api_key_suffix"] == "abcd1234"
    assert mine["system_hash"] == "abc123def456"


def test_session_metadata_coalesces_on_update(storage):
    """Metadata is preserved across upserts when not re-supplied."""
    sid = f"sess-coal-{uuid.uuid4().hex[:12]}"
    storage.touch_session(
        sid, source="pytest",
        client_type="sdk-python",
        user_id="alice@example.com",
    )
    # Second touch without metadata — values should persist.
    storage.touch_session(sid, source="pytest")
    entries = storage.list_sessions(hours=1)
    mine = next(e for e in entries if e["session_id"] == sid)
    assert mine["client_type"] == "sdk-python"
    assert mine["user_id"] == "alice@example.com"
    assert mine["turns"] == 2


def test_rate_limits_roundtrip(storage):
    rid = f"req-{uuid.uuid4().hex[:12]}"
    rl = {
        "ratelimit_requests_limit": 1000,
        "ratelimit_requests_remaining": 950,
        "ratelimit_tokens_limit": 400000,
        "ratelimit_tokens_remaining": 350000,
        "ratelimit_input_tokens_limit": 200000,
        "ratelimit_input_tokens_remaining": 180000,
        "ratelimit_output_tokens_limit": 200000,
        "ratelimit_output_tokens_remaining": 190000,
        "ratelimit_tokens_utilization": 0.125,
        "ratelimit_input_tokens_utilization": 0.1,
        "ratelimit_output_tokens_utilization": 0.05,
    }
    storage.record_rate_limits(
        request_id=rid, provider="anthropic", model="haiku", ratelimit=rl
    )

    current = storage.get_rate_limit_current("anthropic")
    assert current is not None
    assert current["provider"] == "anthropic"
    assert current["model"] == "haiku"
    assert current["tokens_limit"] == 400000
    assert current["tokens_remaining"] == 350000
    assert current["tokens_utilization"] == pytest.approx(0.125)

    trend = storage.get_rate_limit_trend(hours=1, provider="anthropic")
    assert len(trend) >= 1
    assert trend[0]["samples"] >= 1


def test_rate_limits_empty_skipped(storage):
    storage.record_rate_limits(
        request_id="req-noop", provider="anthropic", ratelimit=None
    )
    storage.record_rate_limits(
        request_id="req-noop2", provider="anthropic", ratelimit={}
    )
    assert storage.get_rate_limit_current("anthropic") is None


def test_rate_limits_no_data(storage):
    assert storage.get_rate_limit_current("anthropic") is None
    assert storage.get_rate_limit_trend(hours=1) == []
def test_session_stats_windowed(storage):
    """Windowed stats match windowed entries so the dashboard header and
    table agree; a session outside the window is excluded from both."""
    import time

    recent = f"sess-{uuid.uuid4().hex[:12]}"
    stale = f"sess-{uuid.uuid4().hex[:12]}"
    storage.touch_session(recent, source="pytest")
    storage.touch_session(stale, source="pytest")

    ph = "%s" if type(storage).__name__ == "PostgresStorage" else "?"
    storage.conn.execute(
        f"UPDATE sessions SET ended_at = {ph} WHERE session_id = {ph}",
        (time.time() - 72 * 3600, stale),
    )
    storage.conn.commit()

    windowed = storage.get_session_stats(hours=24)
    assert windowed["sessions"] == len(storage.list_sessions(hours=24))
    listed = {e["session_id"] for e in storage.list_sessions(hours=24)}
    assert recent in listed
    assert stale not in listed
    # Lifetime stats still count the stale session.
    assert storage.get_session_stats()["sessions"] > windowed["sessions"]


class _Cfg:
    class storage:  # noqa: N801 — mimics loaded config shape
        backend = "postgres"
        postgres_dsn = ""
        database_path = "unused.db"


def test_create_storage_factory(tmp_path):
    default = create_storage(None)
    assert isinstance(default, LoomStorage)

    with pytest.raises(ValueError, match="postgres_dsn"):
        create_storage(_Cfg())


def test_cache_tokens_and_skill_roundtrip(storage):
    _record_request(
        storage,
        request_id="req-cache-1",
        cache_read_tokens=1800,
        cache_creation_tokens=250,
        skill="end-session",
    )
    _record_request(storage, request_id="req-cache-2")

    page = storage.get_audit_entries(limit=10)
    by_id = {e["request_id"]: e for e in page["entries"]}

    tagged = by_id["req-cache-1"]
    assert tagged["cache_read_tokens"] == 1800
    assert tagged["cache_creation_tokens"] == 250
    assert tagged["skill"] == "end-session"

    untagged = by_id["req-cache-2"]
    assert untagged["cache_read_tokens"] == 0
    assert untagged["cache_creation_tokens"] == 0
    assert untagged["skill"] is None


def test_audit_skill_filter(storage):
    _record_request(storage, request_id="req-skill-1", skill="end-session")
    _record_request(storage, request_id="req-skill-2", skill="tasks")
    _record_request(storage, request_id="req-skill-3")

    page = storage.get_audit_entries(limit=10, skill="end-session")
    assert page["total"] == 1
    assert page["entries"][0]["request_id"] == "req-skill-1"
