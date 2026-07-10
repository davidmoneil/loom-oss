# Changelog

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
