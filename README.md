# Loom — LLM Optimization Gateway

Loom is a gateway that sits between your applications and LLM providers. It speaks the
standard chat-completions API, then transparently handles intelligent **routing** (via the
EQRT — Empirically Qualified Routing Table — algorithm), **context compression**, and full
**observability** for every request. Point your app at Loom instead of a provider, and you
get cost-aware model selection, token reduction, and an audit trail without changing your
application code.

## Quickstart

```bash
# Install (editable, with dev extras)
pip install -e ".[dev]"

# Create your local config from the example
cp loom.example.yaml loom.yaml
# edit loom.yaml — add provider API keys via env vars, tune sources/routing

# Run the gateway
loom serve
# or directly:
uvicorn loom.gateway.app:app --host 0.0.0.0 --port 4000
```

## Docker Quickstart

```bash
cp loom.example.yaml loom.yaml   # then edit
docker compose up
```

The gateway listens on port `4000` by default.

## Key Endpoints

| Method | Path                       | Purpose                                       |
|--------|----------------------------|-----------------------------------------------|
| POST   | `/v1/chat/completions`     | OpenAI-compatible chat completions (proxied)  |
| GET    | `/health`                  | Liveness/readiness check                      |
| GET    | `/v1/models`               | List models available across configured providers |
| GET    | `/stats`                   | Recent routing + usage statistics             |

## Architecture

Loom is organized into a few focused layers:

- **Routing** (`loom.routing`) — the EQRT algorithm selects a model per request based on
  source policy, required capabilities, determinism targets, and empirical performance.
- **Compression** (`loom.compression`) — reduces prompt/context tokens before forwarding,
  with a cache keyed by content hash and age ratio.
- **Detection** (`loom.detection`) — classifies incoming requests (task type, capability
  needs) to inform routing.
- **Observability** (`loom.observability`) — fire-and-forget JSONL audit and metrics logs,
  backed by a SQLite store (`loom.storage`) for routing decisions, metrics, sessions, and
  the compression cache.

Configuration is loaded from `loom.yaml` (see `loom.example.yaml`) with `LOOM_*` environment
variable overrides.
