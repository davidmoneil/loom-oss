# Configuration Precedence

Loom resolves its configuration in two stages: **file resolution** (which YAML file to
load) and **environment overrides** (a small set of `LOOM_*` variables applied on top of
whatever the file set). This is implemented in `src/loom/config.py`
(`LoomConfig.load()` / `LoomConfig._apply_env_overrides()`).

## 1. File resolution order

`LoomConfig.load()` checks these candidates in order and uses the **first one that
exists**:

1. `$LOOM_CONFIG` (if the env var is set, its path is used — no further fallback if the
   file is missing)
2. `./loom.yaml` (relative to the process's working directory)
3. `/etc/loom/loom.yaml`

If none of the candidates exist, `LoomConfig` falls back to built-in defaults (empty
provider list) so the gateway can still start.

## 2. Environment variable overrides

After the YAML file (or defaults) is loaded, `_apply_env_overrides()` applies a fixed
set of `LOOM_*` variables on top. Only variables that are actually set in the
environment override the loaded value — unset variables leave the YAML value in place.

| Env var | Overrides |
|---|---|
| `LOOM_SERVER_HOST` | `server.host` |
| `LOOM_SERVER_PORT` | `server.port` |
| `LOOM_SERVER_LOG_LEVEL` | `server.log_level` |
| `LOOM_OAUTH_PASSTHROUGH` | `server.oauth_passthrough` (truthy values: `1`, `true`, `yes`, `on`) |
| `LOOM_STORAGE_BACKEND` | `storage.backend` |
| `LOOM_STORAGE_DATABASE_PATH` | `storage.database_path` |
| `LOOM_POSTGRES_DSN` | `storage.postgres_dsn` |
| `LOOM_NEO4J_URI` | `compression.neo4j_uri` (also sets `compression.variant_store` if unset) |
| `LOOM_NEO4J_USER` | `compression.neo4j_user` |
| `LOOM_NEO4J_PASSWORD` | `compression.neo4j_password` |

These are the only fields env vars can override — everything else in `LoomConfig` comes
exclusively from the YAML file.

## 3. `loom.yaml` vs `loom.homelab.yaml`

The repo ships two config files, and the difference is not just cosmetic:

- `loom.yaml` — default/local config. `storage.postgres_dsn` points at
  `postgresql://n8n@postgres-unified:5432/pgvector_db`.
- `loom.homelab.yaml` — homelab-flavored config. `storage.postgres_dsn` points at a
  **different database**, `postgresql://n8n@postgres-unified:5432/loom`.

**Important**: `docker-compose.homelab.yml` sets `LOOM_CONFIG=/app/loom.yaml` — it loads
the *default* file, not `loom.homelab.yaml`. The homelab deployment gets its
homelab-specific Postgres DSN not from `loom.homelab.yaml` but from an explicit
`LOOM_POSTGRES_DSN` environment override baked directly into
`docker-compose.homelab.yml`, which matches the DSN in `loom.homelab.yaml`
(`postgresql://n8n@postgres-unified:5432/loom`).

In other words: for the homelab deployment, `loom.homelab.yaml` is not actually loaded
by the running container — its `postgres_dsn` value is reproduced via env override
instead. Keep the two in sync manually if you change one.

## 4. Effective precedence (highest wins)

1. `LOOM_*` env overrides (§2) — for the 10 fields listed above only
2. `$LOOM_CONFIG` file, if set and present
3. `./loom.yaml`
4. `/etc/loom/loom.yaml`
5. Built-in defaults (empty provider list)

For any config field *not* in the env-override table, only the file resolution order
(§1) applies — there is no env var escape hatch.
