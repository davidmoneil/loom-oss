# Storage & Data Persistence

Loom records every routing decision, token count, cost estimate, and audit
event. This page explains where that data lives, how it survives container
rebuilds, and what to back up.

## Storage backends

Loom supports two storage backends, selected in `loom.yaml` (or via
environment variables):

| Backend | Config | Install | Data location |
|---------|--------|---------|---------------|
| **SQLite** (default) | `storage.backend: sqlite` | included | `data/loom.db` inside the container |
| **PostgreSQL** | `storage.backend: postgres` | `pip install 'loom-gateway[postgres]'` | external Postgres instance |

`setup.sh` walks you through the choice interactively. You can also set it
manually:

```yaml
# loom.yaml
storage:
  backend: postgres                  # sqlite | postgres
  database_path: data/loom.db       # used when backend is sqlite
  postgres_dsn: postgresql://user:pass@host:5432/loom
```

### Environment variable overrides

These override the corresponding `loom.yaml` values at startup:

| Variable | Overrides |
|----------|-----------|
| `LOOM_STORAGE_BACKEND` | `storage.backend` |
| `LOOM_POSTGRES_DSN` | `storage.postgres_dsn` |
| `LOOM_STORAGE_DATABASE_PATH` | `storage.database_path` |

## Docker volumes

The `docker-compose.yml` defines three named volumes:

| Volume | Container path | Contents |
|--------|---------------|----------|
| `loom-data` | `/app/data` | SQLite database (`loom.db`), compression cache |
| `loom-logs` | `/app/logs` | `audit.jsonl`, `metrics.jsonl` (written regardless of backend) |
| `loom-pgdata` | `/var/lib/postgresql/data` | Bundled Postgres data (only when using the `postgres` compose profile) |

### What survives what

| Operation | Volumes | Data |
|-----------|---------|------|
| `docker compose up --build` | kept | **all data intact** |
| `docker compose down` | kept | **all data intact** |
| `docker compose down -v` | **deleted** | SQLite DB and logs lost; external Postgres unaffected |
| `docker compose rm` | kept | **all data intact** |
| Deleting/re-cloning the repo | kept (volumes are Docker-managed, not in the repo) | **all data intact** |

**Key point:** named Docker volumes live in Docker's storage directory
(`/var/lib/docker/volumes/`), not inside the project folder. They persist
independently of the source code. The only command that removes them is
`docker compose down -v` (or `docker volume rm` directly).

## Backend-specific notes

### SQLite

- Zero dependencies — works out of the box.
- All data (routing decisions, metrics, sessions, compression cache) lives in
  a single file at `data/loom.db` inside the `loom-data` volume.
- Good for single-instance deployments and getting started quickly.

### PostgreSQL

Three ways to connect:

1. **Existing Postgres** — provide a DSN via config or `LOOM_POSTGRES_DSN`.
   Tables are created automatically on first connect (auto-migration).
2. **Bundled Postgres** — `setup.sh` option 3, or set
   `COMPOSE_PROFILES=postgres` in `.env`. Starts a `postgres:16-alpine`
   container alongside the gateway with its own `loom-pgdata` volume.
3. **Network-adjacent Postgres** — join an existing Docker network (see
   `docker-compose.homelab.yml` for an example connecting to an n8n Postgres).

When using an external Postgres, the gateway data lives entirely in the
database — `loom-data` only holds a placeholder SQLite file and the
compression cache. Losing the `loom-data` volume in this case has no impact on
request history.

## Logs

Regardless of storage backend, Loom writes two JSONL log files to
`/app/logs/` (the `loom-logs` volume):

- `audit.jsonl` — per-request audit trail (model, tokens, latency, source)
- `metrics.jsonl` — periodic aggregated metrics

These are append-only and grow over time. They are useful for debugging and
external log ingestion but are not the primary data store — the storage
backend (SQLite or Postgres) is the source of truth for the dashboard and
API.

## Backups

| Backend | What to back up |
|---------|----------------|
| SQLite | The `loom-data` Docker volume (or copy `data/loom.db` from inside the container) |
| External Postgres | Your Postgres database (standard `pg_dump`) |
| Bundled Postgres | The `loom-pgdata` Docker volume |
| Logs (optional) | The `loom-logs` Docker volume |

To copy a file out of a named volume:

```bash
docker cp loom-oss-loom-1:/app/data/loom.db ./loom-backup.db
```
