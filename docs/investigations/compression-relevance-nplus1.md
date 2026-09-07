# Compression relevance scoring: N+1 AGE query pattern causing multi-second latency and 504s

**Date**: 2026-08-17
**Status**: confirmed root cause, fix not yet implemented
**Severity**: live-traffic impact — confirmed 504 on this gateway's own `/v1/messages` traffic

## Symptom

The homelab gateway logs `compression phase completed in 6-8 seconds` on
essentially every request for long-running interactive sessions (observed at
roughly one log line per ~10s on an active Claude Code session routed through
`http://localhost:4444`). Local, dependency-free compression (the default
pipeline — `llm_prose: false`) should be regex/string work on an
already-in-memory payload; it should not take multiple seconds.

The same session's audit log (`logs/audit.jsonl`) recorded a live `504` on
`/v1/messages` at `2026-08-17T18:36:45Z` (`source: default`,
`compressed: false`, `tokens_in/out: 0` — the request never completed, so no
compression stats were recorded for it). This is not a one-off: audit history
shows the same `504` pattern recurring since at least 2026-08-10.

## Root cause

`_score_messages_by_relevance()` (`src/loom/gateway/app.py:3308`) is the
relevance-discount step described in `docs/compression.md` (variant-store
section — content indexed by an external context engine gets its effective
age reduced by 0.25). It loops over every message in the conversation and
calls `variants.is_indexed(content_hash)` **once per message**.

For the `age` backend (`src/loom/compression/variants.py`, `is_indexed()` ~
line 237), each call opens/reuses a connection and runs a fresh synchronous
Cypher `MATCH` query against Postgres/AGE — there is no batching and no
caching across the calls in a single request.

This is a classic N+1 query pattern: a session with a few hundred messages
(unremarkable for an active Claude Code coding session — Read/Edit/Bash tool
turns accumulate fast) means a few hundred synchronous DB round-trips,
**every single request**, since Claude Code resends the full conversation
each turn. At even 10-20ms per round-trip, 300-400 messages alone accounts
for the observed 3-8 second compression-phase duration. This runs inside the
`asyncio.to_thread`-offloaded compression pass
(`_compress_messages_inline`), so it doesn't freeze the gateway's event loop,
but it does hold that thread-pool worker for the full duration and adds
multiple seconds of latency to every forwarded request.

Two hardcoded timeouts in `src/loom/gateway/app.py` are relevant:
```
_COMPRESSION_TIMEOUT_SECONDS = 60.0
_UPSTREAM_TIMEOUT_SECONDS = 300.0
```
As a session's message count grows, the linear N+1 cost trends toward the
60s compression-phase ceiling — the `504` observed on this session's own
traffic is consistent with that ceiling (or the upstream ceiling) being hit.

## Why this was hard to see from `/health`

The `/health` compression rollup (`compression_ratio`, `by_block_type`) only
reports on requests that *completed*. A request that 504s mid-compression
contributes nothing to that rollup — `tokens_before`/`tokens_after` stay at
their prior values — so the aggregate stats look normal even while some
fraction of requests are silently failing or degrading latency. This gap was
found by cross-referencing `logs/audit.jsonl` (per-request, includes failed
requests) against live container logs (`docker logs loom-oss-loom-1`), not
from `/health` alone.

## Suggested fix (not yet implemented)

Batch the relevance lookup into a single Cypher query keyed by a list of
content hashes (`WHERE c.content_hash IN [...]`) instead of one query per
message, mirroring how `put_variant`/`get_original` could similarly be
batched if they show the same pattern under load. Should bring the
compression phase back to sub-second for local-only (non-`llm_prose`)
requests regardless of session length.

Until fixed, `variant_store: age` on a long-running interactive source is a
correctness/availability risk, not just a compression-ratio one — sessions
long enough to approach the message-count where N+1 cost meets
`_COMPRESSION_TIMEOUT_SECONDS` will fail outright rather than degrade
gracefully.

## Related

- [docs/compression.md](../compression.md) — variant store / relevance
  scoring design
- [docs/investigations/compression-tool-use-400.md](compression-tool-use-400.md) —
  prior compression-path investigation, same general area of the codebase
