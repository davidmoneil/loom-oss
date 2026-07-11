# Loom — LLM Optimization Gateway

Loom is a gateway that sits between your applications and LLM providers. It speaks the
standard chat-completions API, then transparently handles intelligent **routing** (via the
EQRT — Empirically Qualified Routing Table — algorithm), **context compression**, and full
**observability** for every request. Point your app at Loom instead of a provider, and you
get cost-aware model selection, token reduction, and an audit trail without changing your
application code.

## Quickstart (Docker)

```bash
git clone https://github.com/davidmoneil/loom-oss.git && cd loom-oss
./setup.sh
```

`setup.sh` creates `loom.yaml` from the example, asks which storage backend you
want — SQLite (default), an existing PostgreSQL (you provide the DSN), or a
bundled PostgreSQL started alongside the gateway — writes `.env`, and brings the
stack up with `docker compose`. The gateway listens on port `4444`.

Manual equivalent:

```bash
cp loom.example.yaml loom.yaml   # then edit
docker compose up -d --build
```

## Quickstart (bare Python)

```bash
pip install -e ".[dev]"          # add ,postgres for the Postgres backend
cp loom.example.yaml loom.yaml   # then edit
loom serve
# or directly:
uvicorn loom.gateway.app:app --host 0.0.0.0 --port 4444
```

Requests authenticate to upstream providers with the caller's own credentials
(`Authorization` / `x-api-key` headers are passed through) — the gateway holds
no provider keys.

## Storage backends

| Backend | Selection | Notes |
|---------|-----------|-------|
| SQLite (default) | `storage.backend: sqlite` | zero dependencies; `data/loom.db` |
| PostgreSQL | `storage.backend: postgres` + `postgres_dsn` (or `LOOM_POSTGRES_DSN`) | install extras: `pip install -e ".[postgres]"` |

Data is stored in named Docker volumes (`loom-data`, `loom-logs`) that persist
across image rebuilds and `docker compose down`. See
[docs/storage.md](docs/storage.md) for the full persistence story, volume
layout, environment variable overrides, and backup guidance.

## Key Endpoints

| Method | Path                       | Purpose                                       |
|--------|----------------------------|-----------------------------------------------|
| POST   | `/v1/chat/completions`     | OpenAI-compatible chat completions (proxied)  |
| POST   | `/v1/messages`             | Anthropic-compatible messages (proxied)       |
| GET    | `/health`                  | Liveness + rollup counters                    |
| GET    | `/api/models`              | Models available across configured providers  |
| GET    | `/api/costs`               | Cost/usage/savings aggregates ([contract](docs/observability-api.md)) |
| GET    | `/api/audit`               | Per-request audit trail                       |
| GET    | `/api/sessions`            | Session rollup                                |
| GET    | `/api/metrics`             | Recent routing + usage statistics             |

The observability endpoints follow a stable v1 contract
(`docs/observability-api.md`) so external dashboards and reporting tools are
independent of the gateway implementation.

## Architecture

Loom is organized into a few focused layers:

- **Routing** (`loom.routing`) — the EQRT algorithm selects a model per request based on
  source policy, required capabilities, determinism targets, and empirical performance.
- **Compression** (`loom.compression`) — reduces prompt/context tokens before forwarding,
  with a cache keyed by content hash and age ratio. Fully local/extractive by default
  (four tiers, content-aware compressors, tool-result compression), with optional
  LLM-assisted prose summarization and a Neo4j variant store for relevance-aware
  compression. See [docs/compression.md](docs/compression.md) for the full picture.
- **Detection** (`loom.detection`) — classifies incoming requests (task type, capability
  needs) to inform routing.
- **Observability** (`loom.observability`) — fire-and-forget JSONL audit and metrics logs,
  backed by a pluggable store (`loom.storage`, SQLite or PostgreSQL) for routing decisions,
  metrics, and the compression cache.

Configuration is loaded from `loom.yaml` (see `loom.example.yaml`) with `LOOM_*` environment
variable overrides.

## License

Apache-2.0 — see [LICENSE](LICENSE).
