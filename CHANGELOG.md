# Changelog

## Unreleased

### Features
- **Tool-block compression**: text inside `tool_result` blocks is now compressed in place — block structure (`tool_use_id`, `is_error`, list shape, non-text sub-blocks) is always preserved, and `tool_use` inputs are never touched. Previously any message containing tool blocks was skipped entirely, which left agentic sessions (Claude Code, SDK agents) at ~2% compression; tool results are typically 80–90% of their token volume. Opt out with `compression.tool_results: false`.
- **ModeB segment pre-pass**: large tool outputs get summary+pointer compression for recognized segments (logs, error stacks, JSON arrays, repeated patterns) before graduated compression.
- **Per-block-type savings**: `/health` `compression.by_block_type` breaks tokens before/after/saved down by `tool_result` / `text` / `tool_use` / `message`.
- **Resolved-tier enforcement**: the gateway's inline compression loop now resolves and applies the actual tier (`x-loom-compression` header > per-source policy > `compression.default_tier` > `LOOM_COMPRESSION_TIER` env > `medium`) instead of always compressing at a fixed default, so tier configuration actually takes effect on live requests.
- **Neo4j variant store** (optional, `compression.variant_store: neo4j`): preserves pre-compression originals keyed by content hash for pointer resolution, and enables relevance-aware compression — content also indexed by an external context engine gets its effective age reduced so it's compressed less aggressively. Falls back to a no-op store when unconfigured or unavailable; requires `pip install 'loom-gateway[neo4j]'`.
- **LLM prose compression** (optional, `compression.llm_prose`): summarizes prose via a local Ollama (or OpenAI-compatible) endpoint instead of the extractive first-sentence-per-paragraph default, preserving exit codes, counts, paths, and completion status. Off by default — the pipeline stays fully local/extractive with zero model calls; any endpoint failure falls back to extractive without failing the request.
- **Compression docs**: full reference at `docs/compression.md` covering tiers, content-aware compressors, tool-result compression, LLM prose, the variant store, and the `/health`/`/api/costs` observability contract.

### Fixes
- **`tokens_saved` was wildly overestimated**: `/api/costs` derived savings as `tokens_in * (1/ratio - 1)`, but `tokens_in` is the provider-reported *post-compression* count. Savings are now measured at compression time and stored per request (schema v7, `metrics.tokens_saved`).

## v0.1.0 — 2026-07-10

First public release.

### Features
- **Gateway**: OpenAI-compatible chat-completions API on port 4444; Anthropic Messages API passthrough with streaming, tool use, and prompt-caching header support
- **Routing**: EQRT (Empirically Qualified Routing Table) cost-aware model selection across providers (Anthropic, OpenAI, Gemini, Ollama)
- **Compression**: graduated context compression (light/medium/heavy by message age) with content-aware compressors for logs, git diffs, JSON, code, config, and directory listings; status-signal preservation
- **Observability**: per-request audit trail, token/cost metrics, rate-limit capture (unified + legacy headers), embedded React dashboard
- **Sessions**: multi-signal session fingerprinting from API request data
- **Storage**: SQLite (default) or PostgreSQL (external DSN or bundled via compose profile)
- **Deploy**: `setup.sh` interactive bootstrap + Docker Compose; bare-Python option

### Notes
- Compression is fully local/extractive by default — no network calls from the compressor. LLM-assisted prose summarization is on the roadmap (see `docs/gap-analysis.md`).
