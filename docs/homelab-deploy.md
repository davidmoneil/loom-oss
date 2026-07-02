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
