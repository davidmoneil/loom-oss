"""Compression analytics: storage aggregation (both backends) + endpoint."""

import json
import uuid

from fastapi.testclient import TestClient


def _record(storage, *, compressed=True, ratio=0.4, saved=600, tier="medium",
            model="haiku", source="pytest", before=None, after=None,
            by_block_type=None):
    storage.record_metrics(
        request_id=uuid.uuid4().hex[:12],
        model=model,
        provider="anthropic",
        tokens_in=1000,
        tokens_out=200,
        latency_ms=50.0,
        cost=0.01,
        compressed=compressed,
        compression_ratio=ratio,
        tokens_saved=saved if compressed else 0,
        tier=tier if compressed else None,
        source=source,
        tokens_before=before or 0,
        tokens_after=after or 0,
        by_block_type=json.dumps(by_block_type) if by_block_type else None,
    )


def test_compression_summary_shape_and_math(storage):
    # 3 compressed (60%, 60%, 20% savings) + 1 uncompressed.
    _record(storage, ratio=0.4, saved=600, tier="medium")
    _record(storage, ratio=0.4, saved=600, tier="medium")
    _record(storage, ratio=0.8, saved=200, tier="light", model="sonnet")
    _record(storage, compressed=False)

    out = storage.get_compression_summary(days=1)
    t = out["totals"]
    assert t["requests"] == 4
    assert t["compressed_requests"] == 3
    assert t["tokens_saved"] == 1400
    assert abs(t["mean_savings_pct"] - 46.67) < 0.1
    assert t["median_savings_pct"] == 60.0

    hist = out["ratio_histogram"]
    assert len(hist) == 10
    assert sum(b["count"] for b in hist) == 3
    assert hist[6]["count"] == 2  # 60% savings → 60-70% bucket
    assert hist[2]["count"] == 1  # 20% savings → 20-30% bucket

    tiers = {b["tier"]: b for b in out["by_tier"]}
    assert tiers["medium"]["compressed_requests"] == 2
    assert tiers["medium"]["tokens_saved"] == 1200
    assert tiers["light"]["tokens_saved"] == 200
    assert tiers["uncompressed"]["requests"] == 1

    models = {b["model"]: b for b in out["by_model"]}
    assert models["haiku"]["tokens_saved"] == 1200
    assert models["sonnet"]["tokens_saved"] == 200

    assert len(out["by_day"]) >= 1
    assert out["by_day"][-1]["requests"] == 4


def test_compression_summary_tokens_before_after_and_block_type(storage):
    # Two compressed requests with token counts + per-block-type breakdown,
    # plus one legacy-shaped row (no tokens_before/after/by_block_type,
    # mirroring rows written before schema version 15) that must not break
    # aggregation.
    _record(
        storage, ratio=0.4, saved=600, before=1000, after=400,
        by_block_type={"tool_result": {"before": 700, "after": 200},
                        "text": {"before": 300, "after": 200}},
    )
    _record(
        storage, ratio=0.5, saved=300, before=600, after=300,
        by_block_type={"tool_result": {"before": 600, "after": 300}},
    )
    _record(storage, compressed=False)  # legacy shape: no before/after/block_type

    out = storage.get_compression_summary(days=1)
    t = out["totals"]
    assert t["requests"] == 3
    assert t["tokens_before"] == 1600
    assert t["tokens_after"] == 700
    assert t["compression_ratio"] == round(1.0 - 700 / 1600, 3)

    by_type = out["by_block_type"]
    assert by_type["tool_result"]["tokens_before"] == 1300
    assert by_type["tool_result"]["tokens_after"] == 500
    assert by_type["tool_result"]["tokens_saved"] == 800
    assert by_type["text"]["tokens_before"] == 300
    assert by_type["text"]["tokens_after"] == 200


def test_compression_summary_empty_window(storage):
    out = storage.get_compression_summary(days=1)
    assert out["totals"]["requests"] == 0
    assert out["totals"]["mean_savings_pct"] == 0.0
    assert out["totals"]["tokens_before"] == 0
    assert out["totals"]["tokens_after"] == 0
    assert out["totals"]["compression_ratio"] == 0.0
    assert out["by_tier"] == []
    assert out["by_block_type"] == {}
    assert sum(b["count"] for b in out["ratio_histogram"]) == 0


def test_compression_endpoint(tmp_path):
    from loom.gateway.app import app
    from loom.storage.sqlite import LoomStorage

    gw = app.state.gateway
    prev_storage = gw.storage
    prev_cache = getattr(gw, "_gateway_keys_exist", None)
    storage = LoomStorage(str(tmp_path / "analytics.db"))
    storage.connect()
    gw.storage = storage
    gw._gateway_keys_exist = None
    try:
        _record(storage, ratio=0.5, saved=500)
        resp = TestClient(app).get("/api/metrics/compression?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["window_days"] == 7
        assert data["totals"]["tokens_saved"] == 500
        assert "est_savings_usd" in data["totals"]
        assert all("est_savings_usd" in b for b in data["by_model"])
    finally:
        storage.close()
        gw.storage = prev_storage
        gw._gateway_keys_exist = prev_cache
