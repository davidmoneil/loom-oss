# Loom Feature Gap Analysis: Internal → OSS

**Generated**: 2026-06-27
**Internal Loom**: ~/Code/loom/ (~25K LOC, 115 Python files)
**OSS Loom**: ~/Code/loom-oss/ (~6K LOC, 30 Python files)

## Design Principles

- **Dashboard-first configuration**: Every configuration value must be manageable
  through the web dashboard, not just through code or YAML. The gateway exposes
  CRUD API endpoints for each config domain; the dashboard consumes them. Code
  should accept dynamic values from the API, not hardcode fixed sets. YAML files
  provide initial defaults; the dashboard provides runtime overrides.

## Status Legend
- **PORTED** — Already in loom-oss
- **PORT** — Should be ported to loom-oss
- **PORT-HOMELAB** — Port to homelab branch only (not main/OSS)
- **CUT** — Not needed in loom-oss
- **NEW-OSS** — Exists in loom-oss but not in internal

---

## 1. Gateway / Proxy Layer

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| Anthropic Messages API proxy | `proxy/server.py` (1160 LOC) | `gateway/app.py` /v1/messages | **PORTED** | OSS has unified gateway |
| OpenAI chat completions | — | `gateway/app.py` /v1/chat/completions | **NEW-OSS** | Internal lacks this |
| Ollama-compatible proxy | `gateway/app.py` /api/generate, /api/chat | — | **PORT** | Useful for local model users |
| Cloud backend rerouting | `gateway/cloud_backends.py` (376 LOC) | — | **PORT** | Ollama→cloud when EQRT says so |
| Gemini provider | — | `gateway/providers/gemini.py` (169 LOC) | **NEW-OSS** | |
| Response normalization | — | `_normalize_response()` in gateway | **NEW-OSS** | All providers → OpenAI format |
| Streaming passthrough | `proxy/server.py` _forward_streaming | `gateway/app.py` _wrapped_stream | **PORTED** | |
| Auth passthrough | Both | Both | **PORTED** | Never stores API keys |
| Programmatic search (zero-inference) | `gateway/programmatic_search.py` (332 LOC) | — | **PORT** | Skip LLM for ripgrep-answerable queries |
| Detection engine (tier recommendation) | `gateway/detection_engine.py` (266 LOC) | `detection/engine.py` (198 LOC) | **PORTED** | OSS version is cleaner |

## 2. Routing Engine (EQRT)

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| EQRT algorithm | `determinism/routing.py` (809 LOC) | `routing/models.py` (496 LOC) + `routing/engine.py` (113 LOC) | **PORTED** | OSS version cleaner |
| Provider registry | `determinism/providers.py` (313 LOC) | `routing/providers.py` (208 LOC) | **PORTED** | |
| Persona profiles | `gateway/persona_profiles.yaml` + PersonaProfile class | — | **PORT-HOMELAB** | Homelab-specific |
| Persona routing policies | `gateway/persona_routing.yaml` | — | **PORT-HOMELAB** | Local eligibility gates |
| Source-aware policies | Via personas | `config.py` SourcePolicy | **PORTED** | OSS uses sources, not personas |
| Routing table YAML | `config/routing-table.yaml` | Optional via `routing.routing_table_path` | **PORTED** | |
| Ollama GPU-aware loaded model check | `_get_ollama_loaded_models()` | — | **PORT** | Prefers already-loaded models |
| Task classifier (17 types) | `gateway/app.py` classify_task_type | `gateway/app.py` _classify_task_type | **PORTED** | OSS has 4 types, internal has 17 |

## 3. Compression Engine

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| ContentProcessor core | `context/content_processor.py` (1598 LOC) | `compression/processor.py` (1254 LOC) | **PORTED** | Core logic extracted |
| Mode B compression | `context/mode_b_compression.py` (485 LOC) | `compression/segment.py` (431 LOC) | **PORTED** | Segment classifier |
| 4-tier system (light/medium/heavy/extreme) | `proxy/compression_config.py` (199 LOC) | `compression/tiers.py` + gateway enforcement | **PORTED** | PR #24: header > source policy > default_tier > env; heavy +0.35 age, extreme force + tool_result fingerprint |
| Compression tags (loom:compressed) | `proxy/server.py` _strip_loom_tag/_add_loom_tag | `compression/tiers.py` + gateway | **PORTED** | Tags carry tier + pointer hash; recompression guard in gateway loop |
| Tool-block compression (tool_result in place) | `proxy/server.py` _compress_content_block | `gateway/app.py` _compress_content_blocks | **PORTED** | PR #21: structure preserved, ModeB pre-pass, by_block_type metrics |
| LLM prose compression (Ollama) | `context/content_processor.py:1515` | `compression/processor.py` _llm_compress_prose | **PORTED** | Opt-in (compression.llm_prose), extractive fallback, <think> stripping |
| Variant store / original preservation | `context/graph.py` (Neo4j) | `compression/variants.py` | **PORTED** | PR #22: optional [neo4j] extra, NullVariantStore fallback |
| Postgres compression cache | `context/compression_cache.py` (124 LOC) | SQLite compression_cache table | **PORTED** | SQLite version in OSS |
| Relevance scoring (embedding-based) | `proxy/server.py` _score_messages_by_relevance | `compression/relevance.py` (93 LOC) | **PORTED** | OSS uses SQLite content_importance |
| T2 compressor | `context/t2_compressor.py` (258 LOC) | — | **CUT** | Research artifact |
| Batch compression CLI | `context/batch_compress.py` (178 LOC) | — | **CUT** | Internal tooling |

## 4. DLP Scanner

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| Scanner engine | `scanner/engine.py` (422 LOC) | `scanner/engine.py` (365 LOC) | **PORTED** | |
| Actions (redact/mask/pseudonymize) | `scanner/actions.py` (91 LOC) | `scanner/actions.py` (99 LOC) | **PORTED** | |
| Session-scoped encryption (Fernet) | `scanner/crypto.py` (128 LOC) | — | **PORT** | Postgres-backed keys |
| Postgres pseudonymization | `scanner/pseudonymizer.py` (146 LOC) | In-memory in actions.py | **PORT-HOMELAB** | OSS uses in-memory, homelab uses Postgres |
| Detection metrics | `scanner/metrics.py` (75 LOC) | Inline in engine.py | **PORTED** | |
| 10 regex rules | Both | Both | **PORTED** | |
| Model tag skip | Both | Both | **PORTED** | |
| Log sanitization | Both | Both | **PORTED** | |
| Content logging | Both | Both | **PORTED** | |
| Bearer token rule | Both | Both | **PORTED** | |
| Scanner management API | Both | Both | **PORTED** | |

## 5. Storage

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| SQLite storage | — | `storage/sqlite.py` (580 LOC) | **NEW-OSS** | |
| Postgres (compression cache) | `context/compression_cache.py` | — | **PORT-HOMELAB** | Optional backend |
| Postgres (session store) | `proxy/session_store.py` (138 LOC) | SQLite sessions table | **PORTED** | |
| Postgres (pseudonym maps) | `scanner/pseudonymizer.py` | In-memory | **PORT-HOMELAB** | |
| Postgres (encryption keys) | `scanner/crypto.py` | — | **PORT-HOMELAB** | |
| Content importance table | Postgres loom_embeddings | SQLite content_importance | **PORTED** | Different approach, same signal |
| Neo4j graph | `context/graph.py` (429 LOC) | — | **CUT** | Too heavy for OSS |

## 6. Observability

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| JSONL audit logging | `proxy/server.py` per-request metrics | `observability/logger.py` (170 LOC) | **PORTED** | |
| Pulse dual-write | `proxy/server.py` _enqueue_pulse_event | — | **PORT-HOMELAB** | Homelab Pulse integration |
| Grafana metrics | `context/grafana_metrics.py` (273 LOC) | — | **CUT** | Homelab-specific |
| Session summaries | `gateway/app.py` /session-summary | — | **PORT-HOMELAB** | Nexus integration |
| UTC ISO timestamps | Both | Both | **PORTED** | |
| Display timezone config | — | `config.py` display_timezone | **NEW-OSS** | |

## 7. Dashboard

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| Overview page | — | `dashboard/src/pages/Overview.jsx` | **NEW-OSS** | |
| Metrics page | — | `dashboard/src/pages/Metrics.jsx` | **NEW-OSS** | |
| Audit page | — | `dashboard/src/pages/Audit.jsx` | **NEW-OSS** | |
| Data Protection page | — | `dashboard/src/pages/Scanner.jsx` | **NEW-OSS** | |
| Routing page | — | — | **BUILD** | North-star wireframe exists |
| Models page | — | — | **BUILD** | North-star wireframe exists |
| Compression page | — | — | **BUILD** | North-star wireframe exists |
| Settings page | — | — | **BUILD** | North-star wireframe exists |

## 8. Training & Research (CUT from OSS)

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| LoRA training pipeline | `training/` (8 scripts) | — | **CUT** | RunPod-specific |
| Autotuner | `autotuner/` (918 LOC) | — | **CUT** | Research artifact |
| Fingerprint module | `fingerprint/` (1278 LOC) | — | **CUT** | Research, keep generator only |
| Determinism benchmarks | `determinism/comparison/` (1275 LOC) | — | **CUT** | Research tooling |
| Ollama GPU lock | `ollama_lock.py` (142 LOC) | — | **CUT** | Single-GPU advisory |
| Trace ingestion/query | `context/trace_*.py` (1308 LOC) | — | **CUT** | Nexus-specific |

## 9. Infrastructure (CUT or PORT-HOMELAB)

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| Nexus persona integration | Gateway persona headers | — | **PORT-HOMELAB** | |
| Cost-ledger reading | Detection engine uses it | — | **CUT** | Homelab JSONL format |
| Learn endpoint | `gateway/app.py` /learn | — | **PORT-HOMELAB** | Training feedback loop |
| Session context endpoint | `gateway/app.py` /session-context | — | **PORT-HOMELAB** | |

---

## 10. Design Considerations (Future)

| Feature | Internal | OSS | Status | Notes |
|---------|----------|-----|--------|-------|
| Per-turn model routing | Not implemented | — | **DESIGN** | [Design doc](design/per-turn-routing.md) — route different turns within an interactive session to different models based on complexity. Infrastructure exists (per-request routing in gateway); needs data + policy before implementing. |

---

## Summary

| Category | PORTED | PORT | PORT-HOMELAB | BUILD | CUT | NEW-OSS |
|----------|--------|------|-------------|-------|-----|---------|
| Gateway | 5 | 2 | 0 | 0 | 0 | 3 |
| Routing | 5 | 1 | 2 | 0 | 0 | 0 |
| Compression | 4 | 2 | 0 | 0 | 2 | 0 |
| Scanner | 10 | 1 | 1 | 0 | 0 | 0 |
| Storage | 3 | 0 | 3 | 0 | 1 | 1 |
| Observability | 2 | 0 | 2 | 0 | 1 | 1 |
| Dashboard | 0 | 0 | 0 | 4 | 0 | 4 |
| Training | 0 | 0 | 0 | 0 | 6 | 0 |
| Infrastructure | 0 | 0 | 3 | 0 | 1 | 0 |
| **Total** | **29** | **6** | **11** | **4** | **11** | **9** |

## Priority Order for PORT items

1. **4-tier compression system + tags** — Directly affects token savings quality
2. **Cloud backend rerouting** — Key cost optimization (Ollama→cloud)
3. **Ollama-compatible proxy endpoints** — Local model users need /api/generate + /api/chat
4. **Programmatic search** — Zero-inference routing saves money
5. **Ollama GPU-aware model check** — Performance optimization
6. **Fernet encryption action** — Completes scanner action set

## Priority Order for BUILD items

1. **Settings page** — Users need to configure providers/compression/scanner
2. **Models page** — View/compare model registry
3. **Routing page** — Routing table editor, source policies
4. **Compression page** — Compression savings visualization

## Priority Order for PORT-HOMELAB items

1. **Postgres storage backend** — Your deployment needs it
2. **Persona system** — Maps to source policies but needs persona profiles
3. **Pulse dual-write** — Your observability pipeline
4. **Session/learn endpoints** — Nexus integration
5. **Postgres pseudonymization + encryption keys** — DLP completeness

---

## Status Update — 2026-07-02

Landed since this analysis was written:

- **PORT items 1–4 + governor**: 4-tier compression, cloud rerouting, Ollama
  proxy endpoints, programmatic search, throttle governor (commits `bed3c59`,
  `bea1f01`).
- **Postgres storage backend** (top PORT-HOMELAB item): merged from the
  `homelab` branch into `main` behind a pluggable `create_storage()` factory —
  SQLite default, Postgres via `storage.backend` / `LOOM_POSTGRES_DSN`. The
  `homelab` branch is deleted; homelab deployment is now `main` +
  `loom.homelab.yaml` + `.env.homelab` (both gitignored). Tests cover both
  backends (`tests/test_storage.py`).
- **Observability API v1** (`docs/observability-api.md`): `/api/costs`,
  `/api/audit`, `/api/sessions`, extended `/health` — implemented here and as
  an adapter on the internal proxy, consumed by the Nexus dashboard. This
  replaces file-coupled consumers and makes cutover timing invisible to them.
- **Portability**: `setup.sh`, compose `postgres` profile, `LOOM_PORT`
  override, LICENSE, README overhaul. Repo pushed to private GitHub
  (`davidmoneil/loom-oss`); fresh-clone acceptance test passed (clone → setup
  → request via ollama → visible in `/api/audit`).

Remaining before cutover of interactive traffic (AIProjects-wmhx):

1. **Per-request compression savings recording** — `_record_request` does not
   yet populate `compressed`/`compression_ratio`, so `/api/costs` savings and
   `/health` compression rollups report zeros here.
2. **Session tracking** — `sessions` table exists (Postgres) but nothing
   writes it; `/api/sessions` reports `supported: false`.
3. **`count_tokens` endpoint** — ~0.4% of interactive traffic.
4. **Persona system, Pulse dual-write, session/learn endpoints** — unchanged
   from the list above.

### z69c Parity Items — Completed 2026-07-02

All three cutover blockers resolved:

1. **Per-request compression savings** — `_compress_messages_inline()` now
   measures tokens before/after, writes `loom:compressed:TIER:HASH` tags
   (double-compression prevention across turns), uses the storage compression
   cache, and records `compressed`/`compression_ratio` per-request in metrics.
   Both `/v1/messages` and `/v1/chat/completions` paths handle this identically.
   `/health` compression block shows live rollups; `/api/costs` `tokens_saved`
   and `savings_usd` are real.

2. **Session tracking** — `derive_session_id()` (source + first-user-message
   hash, same derivation as the legacy proxy for cutover continuity) +
   `touch_session()` on both storage backends (schema v4). `/api/sessions`
   reports `supported: true` with entries.

3. **`/v1/messages/count_tokens`** — passthrough to Anthropic upstream with
   full auth relay (x-api-key and OAuth bearer).

Also fixed: Anthropic OAuth tokens (`sk-ant-oat...`) must use
`Authorization: Bearer` + `anthropic-beta: oauth-2025-04-20`, not `x-api-key`
— interactive Claude Code sessions need this for the cutover.

### Compression tool_use fix — 2026-07-10

`_compress_messages_inline()` was flattening list-type `content` containing
`tool_use` or `tool_result` blocks into a JSON string via `json.dumps`. The
Anthropic API requires these as structured lists — the string replacement caused
upstream 400 errors when Claude Code launched parallel tool calls (agents,
subagents) through loom. Messages with tool blocks are now skipped during
compression and passed through untouched. PR #11.

Remaining before cutover (wmhx):

- Persona system (maps to source policies but needs profile port)
- 48h dual-run comparison (`/api/costs` on both ports)
- Point `settings.json` + guard hooks at :4444, decommission :8711
