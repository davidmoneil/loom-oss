# Changelog

## Unreleased

### Features
- **Tool-block compression**: text inside `tool_result` blocks is now compressed in place — block structure (`tool_use_id`, `is_error`, list shape, non-text sub-blocks) is always preserved, and `tool_use` inputs are never touched. Previously any message containing tool blocks was skipped entirely, which left agentic sessions (Claude Code, SDK agents) at ~2% compression; tool results are typically 80–90% of their token volume. Opt out with `compression.tool_results: false`.
- **ModeB segment pre-pass**: large tool outputs get summary+pointer compression for recognized segments (logs, error stacks, JSON arrays, repeated patterns) before graduated compression.
- **Per-block-type savings**: `/health` `compression.by_block_type` breaks tokens before/after/saved down by `tool_result` / `text` / `tool_use` / `message`.

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
