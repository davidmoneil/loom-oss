# Homelab Deployment

The homelab deployment is `main` + two overlay files — no separate branch.

| File | Tracked? | Purpose |
|------|----------|---------|
| `loom.homelab.yaml` | yes (secret-free) | model registry (fable/opus/sonnet/haiku, gpt-4o, qwen3 family), source policies incl. `headless`, Postgres backend selection, DLP scanner config |
| `.env.homelab` | no — copy from `.env.homelab.example` | `LOOM_POSTGRES_DSN` (the deployment's only secret) |
| `docker-compose.homelab.yml` | yes | binds `loom.homelab.yaml`, joins the n8n Postgres network, installs the `postgres` extras |

## Deploy / update

```bash
cd ~/Code/loom-oss
cp .env.homelab.example .env.homelab   # first time only — fill in the DSN
docker compose -f docker-compose.homelab.yml up -d --build
curl -s localhost:4444/health | jq .status
```

The gateway serves the observability API (`docs/observability-api.md`) on
:4444; the Nexus dashboard consumes it via `LOOM_API_URL`.

## Reproducing on a new machine

Clone the repo, create `.env.homelab` with a DSN pointing at your Postgres
(tables are created automatically on first connect), and run the compose
command above. If there is no external Postgres, use `./setup.sh` instead and
pick the bundled-Postgres option (that path uses the default
`docker-compose.yml`, not the homelab overlay).

## Storage DSN: single source of truth

**The DSN must live in exactly one place: `.env.homelab`.** Do not set
`postgres_dsn` in `loom.homelab.yaml` or `LOOM_POSTGRES_DSN` in the compose
file's `environment:` block. The gateway resolves storage as: yaml
`postgres_dsn` first, then the `LOOM_POSTGRES_DSN` env var overrides it
(`config.py::_apply_env_overrides`) — with the DSN defined in multiple
places, whichever you *didn't* edit silently wins.

### Incident: 2026-07-09 "0 requests" dashboard

Commit `e035d54` pointed `loom.homelab.yaml` at a new, empty `loom` database
to fix `sessions.supported=false` — but the running gateway had been writing
to `pgvector_db` all along via a DSN defined elsewhere. On the next container
restart the gateway came up reading the empty database and the dashboard
showed 0 requests for all time windows. History had to be migrated by hand
(six tables; `compression_cache` needed a timestamptz→epoch conversion
because the old table predated the current schema).

### Before changing the DSN — checklist

1. Find where the *running* container actually got its DSN:
   `docker inspect <container> --format '{{range .Config.Env}}{{println .}}{{end}}' | grep LOOM`
   and check the mounted yaml (`docker inspect` → `.Mounts`).
2. Confirm which database currently holds the data:
   `psql <candidate-dsn> -c "SELECT COUNT(*), to_timestamp(MAX(timestamp)) FROM metrics;"`
   A recent MAX(timestamp) marks the live database.
3. If the new DSN points at a different database, migrate first
   (`pg_dump --data-only` the six tables: metrics, sessions,
   routing_decisions, rate_limits, content_importance, compression_cache),
   then flip the DSN, then restart.
4. After restart, verify continuity:
   `curl -s localhost:4444/api/metrics?hours=168 | jq .metrics.request_count`
   — a sudden 0 means the gateway is reading the wrong database.
