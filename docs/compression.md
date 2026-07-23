# Compression

Loom compresses conversation history before forwarding requests to a provider,
so long-running sessions (interactive agents, SDK tool loops) don't pay full
token price for content the model has already seen. Compression is fully
local and extractive by default — no network calls, no model dependency — with
an opt-in LLM-assisted path for prose.

## How it works

On every `/v1/chat/completions` and `/v1/messages` request, messages are
evaluated for compression. Each eligible message is assigned an **age ratio**
— 0.0 for the oldest message, approaching 1.0 for the most recent eligible
one — and compressed proportionally to its age: older content is compressed
harder. Content already carrying a `<!--loom:compressed:TIER:HASH-->` tag is
skipped (double-compression prevention across turns), and a storage-backed
compression cache (keyed by content hash + age ratio) avoids recompressing
identical content seen before.

Compression never changes message or content-block *shape* — only text
payloads. This matters because the Anthropic and OpenAI APIs require
structured `tool_use` / `tool_result` content blocks; naively flattening a
list-type `content` field into a JSON string produces malformed requests that
the provider rejects (see the historical incident in
[docs/gap-analysis.md](gap-analysis.md#compression-tool_use-fix--2026-07-10)).

### Pipeline stages

```
Request in
  │
  ├─ 1. Loop detection — hash recent tool_use blocks, flag if ≥3 identical
  │     calls in the last 16 assistant messages
  │
  ├─ 2. Recency protection — skip the last N messages entirely
  │     (N = tool_result_protect_window, default 6; tripled when loop detected)
  │
  ├─ 3. Age ratio calculation — idx / max(n-1, 1) for each eligible message
  │
  ├─ 4. Relevance scoring (optional) — if variant store is enabled, content
  │     indexed by an external context engine gets its age discounted by 0.25
  │
  ├─ 5. Graduated compression — per content block, route through the
  │     content-type–specific compressor at the tier matching the age ratio
  │
  ├─ 6. Variant storage (optional) — store original text keyed by content
  │     hash for later pointer resolution
  │
  └─ 7. Tag injection — mark compressed blocks with
        <!--loom:compressed:TIER:HASH--> for idempotency
```

## Tiers

| Tier | Age floor | Behavior |
|------|-----------|----------|
| `light` | 0.5 | Filler removal only |
| `medium` (default) | 0.3 | Graduated age-based compression |
| `heavy` | 0.1 | Age ratio shifted +0.35 — medium-age content gets heavy treatment |
| `extreme` | 0.0 | Always heavy, and tool outputs are replaced with fingerprints |

The age floor is the minimum age ratio a message must reach before it's
compressed at all — a lower floor means more of the conversation is eligible.

Tier resolution priority (first match wins):

1. Per-request `x-loom-compression` header
2. Per-source policy (`sources.<name>.compression_tier` in `loom.yaml`)
3. `compression.default_tier` in `loom.yaml`
4. `LOOM_COMPRESSION_TIER` environment variable
5. `medium`

## Recency protection

The `tool_result_protect_window` (default: 6) shields the last N messages
from compression regardless of age ratio. This prevents the destructive
pattern where Claude reads a file, Loom compresses the output before Claude's
next turn uses it, and Claude re-reads the same file — an infinite loop that
wastes tokens.

When the compression loop detector fires (≥3 identical tool calls in the last
16 assistant messages), the protect window is automatically widened by the
`loop_detected_protect_multiplier` (default: 3×), giving 18 messages of
protection. A warning is logged when this happens.

## Content-aware compression

Text is classified before compression and routed to a matching compressor,
each tuned to preserve the information a downstream model actually needs:

| Content type | Preserves |
|---|---|
| Directory listings | File/dir names; drops permission bits and totals |
| Search results | Match context; drops redundant framing |
| Code | Class/function/decorator/import lines and docstring openers |
| API JSON | Schema/keys; truncates deeply-nested or high-entropy values |
| Log output | Errors, warnings, and trace starts/ends; drops routine INFO/DEBUG |
| Git diffs | Highest-scoring hunks (by change density), trimmed context |
| Git log | Commit summaries |
| Config (YAML/JSON-ish) | Key/value lines; drops comments and blank lines |
| Prose | See [LLM prose compression](#llm-prose-compression-opt-in) below |

Status signals (exit codes, pass/fail counts, completion markers) are
extracted and preserved regardless of content type.

## Tool-result block compression

By default (`compression.tool_results: true`), text inside `tool_result`
content blocks is compressed in place — block structure (`tool_use_id`,
`is_error`, list vs. string shape, non-text sub-blocks) is always preserved,
and `tool_use` blocks (the inputs a model chose) are never touched, since the
model needs to see exactly what it called. This matters most for agentic
sessions (Claude Code, SDK agents): tool results are typically 80-90% of a
session's token volume, so skipping them (the historical behavior) left
compression close to a no-op on this workload.

Blocks shorter than 200 characters are left as-is — compression overhead
isn't worth it below that size.

### Mode B segment pre-pass

Before graduated compression runs, large tool outputs are scanned for
recognizable segments — logs, error stacks, JSON arrays, repeated patterns —
and those segments get a summary-plus-pointer treatment ahead of the general
content-type compressors. This catches structure that spans a whole tool
result rather than a single content type.

## LLM prose compression (opt-in)

Extractive prose compression (first sentence of each paragraph) is fast and
dependency-free, but it can drop facts stated later in a paragraph. When
`compression.llm_prose: true`, prose content is instead summarized by a local
model — via a native Ollama endpoint or an OpenAI-compatible one (any
`llm_url` ending in `/v1`) — instructed to preserve exit codes, counts, file
paths, and completion status exactly.

This path is off by default: the default pipeline makes zero model calls.
When enabled, any failure — endpoint unreachable, timeout, empty response, or
output that isn't actually smaller than the input — falls back to extractive
compression silently; the request is never blocked on the local model being
available.

```yaml
compression:
  llm_prose: true
  llm_url: http://localhost:11434   # Ollama; append /v1 for OpenAI-compatible
  llm_model: qwen2.5:7b
  llm_timeout_seconds: 30
```

Reasoning models that wrap deliberation in `<think>...</think>` tags (the
qwen3 family) have those tags stripped from the output automatically.

## Variant store (optional)

The variant store preserves the pre-compression original of every compressed
payload, keyed by content hash, in a graph database. Two things become
possible with it configured:

- **Pointer resolution** — a compressed payload's `loom:compressed` tag
  carries the hash needed to look up the original later (audit, retrieval,
  tier upgrade).
- **Relevance-aware compression** — content that also exists as *curated*
  graph content (written by an external context engine, not by the gateway
  itself) is treated as high-signal: its effective age is reduced by 0.25,
  which can push it below the tier's age floor and skip compression
  entirely.

When unconfigured, unavailable, or the required driver isn't installed, the
gateway transparently falls back to a no-op store and behaves exactly as
without this feature — it's an enhancement, never a request-path dependency.

### Graph schema

```
(c:LoomContent {content_hash, content_id, source, original_text, content_type})
  -[:HAS_COMPRESSED]->
(v:CompressedVariant {variant_id, tier, original_tokens, compressed_tokens, text, content_hash})
```

Nodes created by the gateway have `source = 'gateway'`. Content indexed by an
external context engine (any node with a different source) is considered
"curated" and receives the relevance discount during compression.

### Backends

| Backend | Config | Dependency | Use case |
|---------|--------|------------|----------|
| **AGE** (recommended) | `variant_store: age` | `psycopg[binary]>=3.1` | Same Postgres instance as storage — no extra infrastructure |
| **Neo4j** | `variant_store: neo4j` | `neo4j` driver | Standalone Neo4j — required for Tier 3 GDS algorithms (PageRank, community detection) |
| **(off)** | `variant_store: ""` | none | No variant storage; compression still works, just without pointer resolution or relevance scoring |

#### AGE (Apache AGE)

AGE runs openCypher queries inside PostgreSQL via the [Apache AGE
extension](https://age.apache.org/). It uses the same Postgres instance as
Loom's storage backend, so there's no separate service to deploy.

```yaml
compression:
  variant_store: age
  age_dsn: "postgresql://user@host:5432/loom"   # or env LOOM_AGE_DSN
  # If age_dsn is empty, falls back to storage.postgres_dsn automatically
```

Requirements: PostgreSQL with `pgvector` and `age` extensions,
`shared_preload_libraries = 'age'` in `postgresql.conf`. See
`docker/Dockerfile.postgres-age` for a ready-made image.

#### Neo4j

```yaml
compression:
  variant_store: neo4j
  neo4j_uri: bolt://localhost:7687   # or env LOOM_NEO4J_URI
  neo4j_user: neo4j                  # or env LOOM_NEO4J_USER
  neo4j_password: ""                 # or env LOOM_NEO4J_PASSWORD
```

## Compression loop detection

The gateway monitors for compression-induced read loops — when Claude
repeatedly re-reads the same file because its output was compressed away
before it could act on it. Detection works by hashing `(tool_name, input)`
for recent `tool_use` blocks in assistant messages. If any hash appears 3 or
more times in the last 16 assistant messages, the conversation is flagged as
looping.

When a loop is detected:
- The recency protect window is widened by `loop_detected_protect_multiplier`
  (default 3×, so 6 → 18 messages protected)
- A warning is logged: `"compression loop detected — widening protect window"`

This is a runtime safety net, not a configuration knob. If loops are
appearing frequently, the `tool_result_protect_window` should be increased.

## Observability

`GET /health` reports a live rollup for this process's lifetime:

```json
"compression": {
  "enabled": true,
  "default_tier": "medium",
  "tokens_before": 128000,
  "tokens_after": 41000,
  "tokens_saved": 87000,
  "compression_ratio": 0.680,
  "by_block_type": {
    "message": {"tokens_before": 12000, "tokens_after": 9000, "tokens_saved": 3000},
    "text": {"tokens_before": 8000, "tokens_after": 6000, "tokens_saved": 2000},
    "tool_result": {"tokens_before": 108000, "tokens_after": 26000, "tokens_saved": 82000}
  }
}
```

`by_block_type` breaks savings down by where the tokens came from — this is
usually where you'll see that `tool_result` dominates for agentic workloads.

`GET /api/costs` reports `tokens_saved` and `savings_usd` per-request,
measured at compression time (not derived after the fact from
provider-reported counts, which reflect the *post-compression* size — see the
[Unreleased changelog entry](../CHANGELOG.md#unreleased) for why that
distinction matters). See [docs/observability-api.md](observability-api.md)
for the full contract.

## Configuration reference

All keys live under `compression:` in `loom.yaml`; see
[loom.example.yaml](../loom.example.yaml) for a fully-commented starting
point. These settings are also editable from the dashboard at
**Settings → Compression**.

| Key | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Master on/off switch |
| `default_tier` | `medium` | Tier used absent a header or source policy override |
| `tool_results` | `true` | Compress text inside `tool_result` blocks |
| `tool_result_protect_window` | `6` | Number of most-recent messages shielded from compression |
| `loop_detected_protect_multiplier` | `3` | Multiplier applied to protect window when loop is detected |
| `llm_prose` | `false` | Route prose through a local LLM instead of extractive compression |
| `llm_url` | `http://localhost:11434` | Ollama or OpenAI-compatible (`/v1`) endpoint |
| `llm_model` | `qwen2.5:7b` | Model name passed to the endpoint |
| `llm_timeout_seconds` | `30.0` | Request timeout before falling back to extractive |
| `variant_store` | `""` (off) | `""`, `"age"`, or `"neo4j"` |
| `age_dsn` | `""` | AGE Postgres DSN; falls back to `storage.postgres_dsn` if empty |
| `neo4j_uri` / `neo4j_user` / `neo4j_password` / `neo4j_database` | `""` / `""` / `""` / `neo4j` | Neo4j connection, only used when `variant_store: neo4j` |
