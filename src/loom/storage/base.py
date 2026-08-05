"""Storage contract shared by the SQLite and Postgres backends.

Every public capability the gateway relies on is declared here, and
``create_storage()`` asserts conformance at startup, so a method added to one
backend but not the other fails immediately instead of surfacing as a
runtime AttributeError (or worse, a silently skipped feature — the original
gateway-key auth shipped Postgres-only and the capability probe quietly
disabled authentication on SQLite installs).

``tests/test_storage_contract.py`` enforces the same thing statically, so
drift is caught in CI without a running gateway.
"""

from __future__ import annotations

import statistics
import time
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    # ---- lifecycle ----
    def connect(self) -> None: ...
    def close(self) -> None: ...
    def migrate(self) -> None: ...

    # ---- per-request recording ----
    def record_metrics(self, **kwargs: Any) -> None: ...
    def record_routing_decision(self, **kwargs: Any) -> None: ...
    def record_rate_limits(self, **kwargs: Any) -> None: ...
    def touch_session(self, **kwargs: Any) -> None: ...

    # ---- content importance ----
    def record_content_importance(self, content_hash: str, source: str = "request") -> None: ...
    def get_content_importance(self, content_hashes: list[str]) -> dict[str, float]: ...
    def cleanup_stale_importance(self, max_age_hours: int = 168) -> int: ...

    # ---- compression cache ----
    def get_compression_cached(self, content_hash: str, age_ratio: float) -> Optional[dict]: ...
    def put_compression_cached(
        self,
        content_hash: str,
        age_ratio: float,
        compressed: str,
        tier: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None: ...
    def cache_stats(self) -> dict: ...
    def cleanup_expired_cache(self) -> int: ...

    # ---- observability aggregates ----
    def get_routing_stats(self, hours: int = 24) -> dict: ...
    def get_compression_summary(self, days: int = 30) -> dict: ...
    def get_routing_decisions(self, hours: int = 24, limit: int = 200) -> dict: ...
    def get_session_stats(self, hours: int | None = None) -> dict: ...
    def list_sessions(self, hours: int = 24, limit: int = 200) -> list[dict]: ...
    def get_cost_summary(self, days: int = 30) -> dict: ...
    def get_metrics_timeseries(self, hours: int = 24, bucket: str = "1h") -> list[dict]: ...
    def get_audit_entries(self, **kwargs: Any) -> list[dict]: ...
    def get_rate_limit_current(self, provider: str = "anthropic") -> Optional[dict]: ...
    def get_rate_limit_trend(self, hours: int = 24, provider: str = "anthropic") -> list[dict]: ...

    # ---- gateway keys ----
    def create_gateway_key(self, name: str) -> dict: ...
    def validate_gateway_key(self, raw_key: str) -> Optional[dict]: ...
    def list_gateway_keys(self) -> list[dict]: ...
    def toggle_gateway_key(self, key_id: int, enabled: bool) -> bool: ...
    def delete_gateway_key(self, key_id: int) -> bool: ...


HISTOGRAM_BUCKETS = 10


def _summarize_compression(records: list[dict], days: int) -> dict:
    """Shared aggregation behind get_compression_summary.

    Both backends fetch the window's per-request rows and delegate here, so
    the statistics/histogram/breakdown logic cannot drift between them.

    ``records``: dicts with compressed, compression_ratio (after/before),
    tokens_saved, tier, model, source, timestamp.

    Savings percentages are reported as (1 - after/before) * 100 — "what
    fraction of the context was removed" — over compressed requests only.
    """
    compressed = [
        r for r in records
        if r["compressed"] and r["compression_ratio"] is not None
    ]
    savings = [max(0.0, 1.0 - r["compression_ratio"]) for r in compressed]
    tokens_saved_total = sum(r["tokens_saved"] for r in records)

    histogram = [
        {"range": f"{b * 100 // HISTOGRAM_BUCKETS}-{(b + 1) * 100 // HISTOGRAM_BUCKETS}%", "count": 0}
        for b in range(HISTOGRAM_BUCKETS)
    ]
    for s in savings:
        # Epsilon guards float artifacts at bucket edges: 1 - 0.8 is
        # 0.1999..., which must count as 20% (bucket 2), not 19.99% (bucket 1).
        bucket = min(int(s * HISTOGRAM_BUCKETS + 1e-9), HISTOGRAM_BUCKETS - 1)
        histogram[bucket]["count"] += 1

    def _breakdown(key: str) -> list[dict]:
        groups: dict[str, dict] = {}
        for r in records:
            label = r[key] or ("uncompressed" if key == "tier" else "unknown")
            g = groups.setdefault(
                label,
                {key: label, "requests": 0, "compressed_requests": 0,
                 "tokens_saved": 0, "_savings": []},
            )
            g["requests"] += 1
            g["tokens_saved"] += r["tokens_saved"]
            if r["compressed"] and r["compression_ratio"] is not None:
                g["compressed_requests"] += 1
                g["_savings"].append(max(0.0, 1.0 - r["compression_ratio"]))
        out = []
        for g in groups.values():
            sv = g.pop("_savings")
            g["mean_savings_pct"] = round(statistics.mean(sv) * 100, 2) if sv else 0.0
            out.append(g)
        out.sort(key=lambda g: g["tokens_saved"], reverse=True)
        return out

    by_day: dict[str, dict] = {}
    for r in records:
        day = time.strftime("%Y-%m-%d", time.gmtime(r["timestamp"]))
        d = by_day.setdefault(
            day, {"day": day, "requests": 0, "compressed_requests": 0, "tokens_saved": 0}
        )
        d["requests"] += 1
        d["tokens_saved"] += r["tokens_saved"]
        if r["compressed"]:
            d["compressed_requests"] += 1

    return {
        "window_days": days,
        "totals": {
            "requests": len(records),
            "compressed_requests": len(compressed),
            "tokens_saved": tokens_saved_total,
            "mean_savings_pct": round(statistics.mean(savings) * 100, 2) if savings else 0.0,
            "median_savings_pct": round(statistics.median(savings) * 100, 2) if savings else 0.0,
            "stdev_savings_pct": (
                round(statistics.stdev(savings) * 100, 2) if len(savings) > 1 else 0.0
            ),
        },
        "ratio_histogram": histogram,
        "by_tier": _breakdown("tier"),
        "by_model": _breakdown("model"),
        "by_source": _breakdown("source"),
        "by_day": sorted(by_day.values(), key=lambda d: d["day"]),
    }
