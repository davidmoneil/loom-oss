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

import json
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
    def get_session_detail(self, session_id: str) -> Optional[dict]: ...
    def get_cost_summary(self, days: int = 30) -> dict: ...
    def get_metrics_timeseries(self, hours: int = 24, bucket: str = "1h") -> list[dict]: ...
    def get_audit_entries(self, **kwargs: Any) -> list[dict]: ...
    def get_rate_limit_current(self, provider: str = "anthropic", auth_type: Optional[str] = None, credential_id: Optional[str] = None) -> Optional[dict]: ...
    def get_rate_limit_trend(self, hours: int = 24, provider: str = "anthropic", auth_type: Optional[str] = None, credential_id: Optional[str] = None) -> list[dict]: ...
    def get_rate_limit_credentials(self, provider: str = "anthropic") -> list[dict]: ...

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
    tokens_saved, tier, model, source, timestamp, and optionally
    skip_reasons (a JSON text blob of per-stage skip/apply counters — see
    ``_compress_messages_inline``'s ``stats`` param), tokens_before,
    tokens_after, and by_block_type (a JSON text blob of
    ``{block_type: {"before": int, "after": int}}``). The token/block-type
    fields are optional for backward compatibility with rows recorded
    before those columns existed; missing values are treated as 0/absent.

    Savings percentages are reported as (1 - after/before) * 100 — "what
    fraction of the context was removed" — over compressed requests only.
    """
    compressed = [
        r for r in records
        if r["compressed"] and r["compression_ratio"] is not None
    ]

    skip_reasons_totals: dict[str, int] = {}
    for r in records:
        raw = r.get("skip_reasons")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        for key, value in parsed.items():
            if isinstance(value, bool):
                if value:
                    skip_reasons_totals[key] = skip_reasons_totals.get(key, 0) + 1
            elif isinstance(value, (int, float)):
                skip_reasons_totals[key] = skip_reasons_totals.get(key, 0) + value
    savings = [max(0.0, 1.0 - r["compression_ratio"]) for r in compressed]
    tokens_saved_total = sum(r["tokens_saved"] for r in records)
    tokens_before_total = sum(r.get("tokens_before") or 0 for r in records)
    tokens_after_total = sum(r.get("tokens_after") or 0 for r in records)

    by_block_type: dict[str, dict] = {}
    for r in records:
        raw = r.get("by_block_type")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        for block_type, counts in parsed.items():
            if not isinstance(counts, dict):
                continue
            g = by_block_type.setdefault(
                block_type, {"tokens_before": 0, "tokens_after": 0}
            )
            g["tokens_before"] += counts.get("before", 0) or 0
            g["tokens_after"] += counts.get("after", 0) or 0
    for g in by_block_type.values():
        g["tokens_saved"] = g["tokens_before"] - g["tokens_after"]

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
            "tokens_before": tokens_before_total,
            "tokens_after": tokens_after_total,
            "compression_ratio": (
                round(1.0 - tokens_after_total / tokens_before_total, 3)
                if tokens_before_total > 0
                else 0.0
            ),
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
        "by_block_type": dict(sorted(by_block_type.items())),
        "skip_reasons": skip_reasons_totals,
        "calls_per_prompt": _calls_per_prompt(records),
    }


def _calls_per_prompt(records: list[dict]) -> dict:
    """Tool calls made between consecutive user prompts.

    Each request row carries ``skip_reasons.turn`` (user_prompts,
    tool_calls_this_turn). Rows of one session that share a user_prompts
    count belong to the same turn; the turn's size is the largest
    tool_calls_this_turn seen for it. Rows without a session id can't be
    grouped and are skipped.
    """
    turns: dict[tuple, int] = {}
    for r in records:
        sid = r.get("session_id")
        raw = r.get("skip_reasons")
        if not sid or not raw:
            continue
        try:
            turn = json.loads(raw).get("turn")
        except (TypeError, ValueError, AttributeError):
            continue
        if not isinstance(turn, dict):
            continue
        key = (sid, turn.get("user_prompts", 0))
        calls = int(turn.get("tool_calls_this_turn", 0) or 0)
        if calls > turns.get(key, -1):
            turns[key] = calls
    sizes = sorted(turns.values())
    if not sizes:
        return {"turns": 0, "sessions": 0, "avg": 0.0, "p50": 0, "p90": 0, "max": 0}

    def _pct(q: float) -> int:
        return sizes[min(len(sizes) - 1, int(q * (len(sizes) - 1) + 0.5))]

    return {
        "turns": len(sizes),
        "sessions": len({k[0] for k in turns}),
        "avg": round(statistics.mean(sizes), 2),
        "p50": _pct(0.5),
        "p90": _pct(0.9),
        "max": sizes[-1],
    }


def _summarize_session(session_id: str, rows: list[dict]) -> Optional[dict]:
    """Per-session stats for /api/sessions/{id}/stats.

    ``rows``: the session's metrics rows, oldest first, with tokens_in,
    tokens_out, cost, tokens_saved, compressed, cache_read_tokens,
    skip_reasons (JSON text), timestamp, model. Shared by both backends.
    """
    if not rows:
        return None
    requests = len(rows)
    tokens_in = sum(r.get("tokens_in") or 0 for r in rows)
    tokens_out = sum(r.get("tokens_out") or 0 for r in rows)
    cache_read = sum(r.get("cache_read_tokens") or 0 for r in rows)
    saved = sum(r.get("tokens_saved") or 0 for r in rows)
    before = sum(r.get("tokens_before") or 0 for r in rows)
    compressed = sum(1 for r in rows if r.get("compressed"))
    applied_heavy = 0
    turn: dict = {}
    for r in rows:
        try:
            sk = json.loads(r.get("skip_reasons") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(sk, dict):
            continue
        applied_heavy += int(sk.get("applied_heavy", 0) or 0) + int(sk.get("applied_extreme", 0) or 0)
        if isinstance(sk.get("turn"), dict):
            turn = sk["turn"]
    last = rows[-1]
    return {
        "session_id": session_id,
        "requests": requests,
        "user_prompts": int(turn.get("user_prompts", 0) or 0),
        "tool_calls": int(turn.get("tool_calls", 0) or 0),
        "tool_calls_this_turn": int(turn.get("tool_calls_this_turn", 0) or 0),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_read_tokens": cache_read,
        "cost": round(sum(r.get("cost") or 0.0 for r in rows), 4),
        "compressed_requests": compressed,
        "evictions": applied_heavy,
        "tokens_saved": saved,
        "saved_pct": round(100.0 * saved / before, 1) if before else 0.0,
        "first_seen": rows[0].get("timestamp"),
        "last_seen": last.get("timestamp"),
        "last_model": last.get("model"),
    }
