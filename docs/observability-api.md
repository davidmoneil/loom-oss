# Loom Observability API — v1 Contract

Stable HTTP contract for external consumers (Nexus dashboard, taskboard, budget
and reporting tooling). Both the loom-oss gateway and the legacy internal proxy
expose these endpoints identically, so consumers never read log files or
databases directly, and switching the underlying proxy is invisible to them.

Rules:

- Consumers MUST tolerate additional fields (implementations may extend).
- Fields marked *optional* may be absent or zero where a backend lacks the
  capability; the shape is still returned.
- All timestamps are Unix seconds (float) unless a field is named `*_iso`.
- No authentication is assumed (bind to localhost / trusted network).

## GET /health

Liveness plus rollup counters.

```json
{
  "status": "healthy",
  "version": "0.4.0",
  "uptime_seconds": 12345.6,
  "requests": 13006,
  "errors": 2,
  "compression": {
    "enabled": true,
    "default_tier": "medium",
    "tokens_before": 0,
    "tokens_after": 0,
    "tokens_saved": 0,
    "compression_ratio": 0.0
  },
  "sessions": { "supported": false, "sessions": 0, "total_turns": 0 }
}
```

`compression` and `sessions` blocks are optional capabilities: always present,
zeroed when unsupported. Implementations may add extra blocks (`scanner`,
`governor`, `providers`, ...).

## GET /api/costs?days=30

Cost / usage / savings aggregates for the reporting window.

```json
{
  "window_days": 30,
  "totals": {
    "requests": 1200,
    "tokens_in": 900000,
    "tokens_out": 120000,
    "cost_usd": 14.52,
    "tokens_saved": 250000,
    "savings_usd": 0.75
  },
  "by_model":  [ { "model": "haiku", "requests": 800, "tokens_in": 1, "tokens_out": 1, "cost_usd": 1.0, "tokens_saved": 0, "savings_usd": 0.0 } ],
  "by_source": [ { "source": "ai-david", "requests": 400, "tokens_in": 1, "tokens_out": 1, "cost_usd": 1.0, "tokens_saved": 0, "savings_usd": 0.0 } ],
  "by_tier":   [ { "tier": "medium", "requests": 900, "tokens_saved": 200000, "savings_usd": 0.6 } ],
  "by_day":    [ { "date": "2026-07-01", "requests": 40, "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.5, "tokens_saved": 0, "savings_usd": 0.0 } ],
  "by_hour":   [ { "hour": "2026-07-02T13:00:00Z", "requests": 5, "tokens_saved": 1200 } ]
}
```

- `source` is the calling identity (persona, hook, job name) as recorded per
  request; internal proxy maps its `persona` field here.
- `by_tier` is optional — empty where compression tier is not recorded per
  request.
- `by_hour` covers the trailing 24h regardless of `days`.
- `savings_usd` is estimated from input-token pricing for the model that served
  the request; `0` where a backend does not record compression savings.

## GET /api/audit?limit=50&offset=0&model=&source=&search=

Per-request audit page, newest first.

```json
{
  "total": 13006,
  "offset": 0,
  "limit": 50,
  "entries": [
    {
      "timestamp": 1751462000.1,
      "request_id": "abc123",
      "source": "ai-david",
      "model": "haiku",
      "requested_model": "sonnet",
      "task_type": "chat",
      "tokens_in": 900,
      "tokens_out": 120,
      "latency_ms": 850.0,
      "cost_usd": 0.004,
      "compressed": true,
      "compression_ratio": 0.62,
      "routing_reason": "tier-downgrade"
    }
  ]
}
```

Filters are AND-combined; `search` substring-matches request id, source, model
and reason fields. Fields not tracked by a backend are `null`.

## GET /api/sessions?hours=24

Session rollup (optional capability). `sessions`, `total_turns` and `entries`
are all scoped to the `hours` window (default 24) so the dashboard header
counters and the entries table always agree.

```json
{
  "supported": true,
  "hours": 24,
  "sessions": 12,
  "total_turns": 340,
  "entries": [
    { "session_id": "s-1", "source": "interactive", "turns": 40, "last_seen": 1751462000.1 }
  ]
}
```

Backends without session tracking return `supported: false` with zeroed
counters and `entries: []`. The lifetime session totals remain available in the
`sessions` block of `GET /health`.

## Implementations

| Backend | Basis |
|---------|-------|
| loom-oss gateway (this repo) | storage backend (SQLite/Postgres) — `src/loom/gateway/app.py` |
| internal loom proxy (legacy) | `requests.jsonl` + session store + model-pricing.yaml — thin adapter in `proxy/server.py` |

Known loom-oss gaps (tracked in `docs/gap-analysis.md`): per-request
compression savings and session tracking are not yet recorded, so `tokens_saved`,
`savings_usd`, `by_tier` and `/api/sessions` report zeros/unsupported until that
parity work lands.
