# Loom — LLM Optimization Gateway

Loom is a gateway that sits between your applications and LLM providers. It speaks the
standard chat-completions API, then transparently handles intelligent **routing** (via the
EQRT — Empirically Qualified Routing Table — algorithm), **context compression**, and full
**observability** for every request. Point your app at Loom instead of a provider, and you
get cost-aware model selection, token reduction, and an audit trail without changing your
application code.

## 100% Local — Private by Design

Loom runs entirely on your machine. There is **no telemetry, no analytics, and no
phone-home** — the codebase contains no tracking of any kind. The only outbound network
traffic Loom ever produces is the LLM API calls **you** configure (Anthropic, OpenAI,
etc.). Point it at a local provider like Ollama and Loom is fully offline. All request
audit data, metrics, and configuration live in your own Postgres database and config
files — nothing leaves your network.

## Quickstart (Docker)

```bash
git clone https://github.com/davidmoneil/loom-oss.git && cd loom-oss
./setup.sh
```

`setup.sh` creates `loom.yaml` from the example, asks which storage backend you
want — SQLite (default), an existing PostgreSQL (you provide the DSN), or a
bundled PostgreSQL started alongside the gateway — writes `.env`, and brings the
stack up with `docker compose`. The gateway listens on port `4444`.

Works on Linux and macOS (including the stock bash 3.2 on macOS). On Windows,
run it from **WSL2 or Git Bash** — Docker Desktop for Windows already requires
WSL2, so no extra setup is needed.

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

## Web Dashboard

The gateway serves a built-in React dashboard at its root URL (`http://localhost:4444/`)
— no separate service to run. Pages: Overview, Audit (per-request trail with full
prompt/response detail), Sessions, Costs, Metrics, Models, Routing, Rate Limits,
Governor, Data Protection (Scanner), and Settings.

Some sections are marked **In Planning** in the UI — the page shows live data but part
of its functionality (e.g., editing configuration from the dashboard) is still being
built. Each badge links to the tracked GitHub issue explaining current state and what's
planned.

## Logging

Every module gets its logger via `loom.logging_setup.get_logger(__name__)`, which
routes through a single `logging.config.dictConfig` set up at process start
(`configure_logging` in `src/loom/logging_setup.py`). Uvicorn's own
`uvicorn`/`uvicorn.access`/`uvicorn.error` loggers are configured the same way, so
gateway and access logs share one level, format, and destination.

Configure it under `server:` in `loom.yaml` (or `loom.homelab.yaml`):

```yaml
server:
  log_level: info          # debug | info | warning | error
  log_format: plain        # plain | json
  log_destination: stderr  # stderr | file
  log_file: logs/loom.log  # used when log_destination is "file"
```

These fields are also editable live from the dashboard (Settings → Server
Settings) via `PATCH /api/config/server` — changes take effect immediately
without a restart.

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
- **Data protection** (`loom.scanner`) — detection/redaction rules, pseudonymization, and
  encryption helpers applied to logged content.
- **Governor** (`loom.governor`) — budget/limit configuration and spend tracking
  (enforcement is in planning, see the dashboard badge).
- **Gateway + Dashboard** (`loom.gateway`, `dashboard/`) — the FastAPI app that ties the
  layers together and serves the React dashboard.

Configuration is loaded from `loom.yaml` (see `loom.example.yaml`) with `LOOM_*` environment
variable overrides.

## License

Loom is **source-available** under the [PolyForm Internal Use License 1.0.0](LICENSE) —
not an OSI-approved open source license.

In plain English:

- **You can** use, run, and modify Loom for your own personal use, or for your
  organization's internal use (including internal business use).
- **You cannot** redistribute Loom, sell it, embed it in a product, or offer it to
  third parties as a hosted or managed service.

If you want to use Loom in a way the license doesn't permit (redistribution,
commercial products, hosting for others), open an issue or contact the author for
a separate license.
