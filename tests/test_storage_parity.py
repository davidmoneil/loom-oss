"""Behavioral parity for the methods that historically diverged between
backends: routing decisions, compression-cache stats/cleanup, gateway keys.

Runs on SQLite always and Postgres when LOOM_TEST_POSTGRES_DSN is set
(see conftest.py).
"""

import time


def test_routing_decisions_shape(storage, unique_id):
    storage.record_routing_decision(
        request_id=unique_id,
        source="pytest",
        task_type="test",
        model="haiku",
        reason="unit-test",
        model_recommended="sonnet",
        determinism_score=0.5,
        alternatives=["sonnet"],
    )
    out = storage.get_routing_decisions(hours=1)
    assert out["total"] >= 1
    assert out["overrides"] >= 1  # recommended != used
    entry = out["entries"][0]
    assert entry["request_id"] == unique_id
    assert isinstance(entry["alternatives"], list)
    assert "alternatives_json" not in entry
    assert isinstance(out["by_reason"], dict)


def test_cache_stats_and_cleanup(storage):
    storage.put_compression_cached("hash-a", 0.5, "compressed text", "medium", 100, 40)
    hit = storage.get_compression_cached("hash-a", 0.5)
    assert hit is not None
    assert hit["compressed_text"] == "compressed text"

    stats = storage.cache_stats()
    assert stats["entries"] == 1
    assert stats["total_hits"] >= 1
    assert stats["total_tokens_saved"] == 60

    # Nothing is expired yet, so cleanup removes nothing.
    assert storage.cleanup_expired_cache() == 0
    assert storage.cache_stats()["entries"] == 1


def test_expired_cache_entries_invisible_and_removable(storage):
    storage.put_compression_cached("hash-b", 0.5, "old", "light", 10, 5)
    # Force-expire the row (portable across both backends' expiry types).
    if hasattr(storage, "_write_lock"):  # sqlite: REAL epoch seconds
        with storage._write_lock:
            storage.conn.execute(
                "UPDATE compression_cache SET expires_at = ?", (time.time() - 10,)
            )
            storage.conn.commit()
    else:  # postgres: TIMESTAMPTZ
        storage.conn.execute(
            "UPDATE compression_cache SET expires_at = NOW() - INTERVAL '1 minute'"
        )

    assert storage.get_compression_cached("hash-b", 0.5) is None
    assert storage.cache_stats()["entries"] == 0
    assert storage.cleanup_expired_cache() == 1


def test_gateway_key_lifecycle(storage):
    created = storage.create_gateway_key("parity")
    assert created["key"].startswith("loom-")
    assert storage.validate_gateway_key(created["key"])["name"] == "parity"

    listed = storage.list_gateway_keys()
    assert len(listed) == 1
    assert listed[0]["key_preview"].endswith("...")
    assert "key" not in listed[0]

    assert storage.toggle_gateway_key(created["id"], False)
    assert storage.validate_gateway_key(created["key"]) is None
    assert storage.toggle_gateway_key(created["id"], True)
    assert storage.validate_gateway_key(created["key"]) is not None

    assert storage.delete_gateway_key(created["id"])
    assert storage.list_gateway_keys() == []


def test_tool_actions_and_action_signatures(storage, unique_id):
    storage.record_metrics(
        request_id=unique_id, model="m", provider="anthropic", tokens_in=1,
        tokens_out=1, latency_ms=1.0, cost=0.0, session_id="s-actions",
        tool_names="Bash,Read", tool_actions="vcs.commit,file.read",
    )
    storage.record_action_signatures([
        ("Bash", "bash.other", "frobnicate run"),
        ("Bash", "bash.other", "frobnicate run"),
        ("Bash", "vcs.commit", "git commit"),  # matched labels are not stored
    ])
    if hasattr(storage, "flush"):
        storage.flush()
    row = storage.conn.execute(
        "SELECT tool_actions FROM metrics WHERE request_id = %s" % ("%s" if storage.__class__.__name__ == "PostgresStorage" else "?"),
        (unique_id,),
    ).fetchone()
    assert tuple(row)[0] == "vcs.commit,file.read"
    rows = storage.conn.execute(
        "SELECT signature, tool, count, sample_label, first_seen <= last_seen "
        "FROM action_signatures"
    ).fetchall()
    assert [tuple(r)[:4] for r in rows] == [("frobnicate run", "Bash", 2, "bash.other")]
