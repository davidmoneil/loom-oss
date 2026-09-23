# Homelab Deployment

The homelab deployment is `main` + two overlay files — no separate branch.

| File | Tracked? | Purpose |
|------|----------|---------|
| `loom.homelab.yaml` | no — untracked local, in `.gitignore` (secret-free, but reveals internal topology) | model registry (fable/opus/sonnet/haiku, gpt-4o, qwen3 family), source policies incl. `headless`, Postgres backend selection, DLP scanner config |
| `.env.homelab` | no — copy from `.env.homelab.example` | `LOOM_POSTGRES_DSN` (the deployment's only secret) |
| `docker-compose.homelab.yml` | no — untracked local, in `.gitignore` | binds `loom.homelab.yaml`, joins the n8n Postgres network, installs the `postgres` extras |

## Deploy / update

**Always pass `-f docker-compose.homelab.yml` explicitly.** This repo also
has a plain `docker-compose.yml` (bundled SQLite/local-Postgres, no external
network) for the public/`setup.sh` path. Running bare `docker compose build`
or `docker compose up -d` with no `-f` flag silently targets that file
instead — it builds and starts happily, reports "healthy", but the container
never joins `n8n_n8n-network` and can't reach `postgres-unified` at all. See
the 2026-09-15 incident below for what that looks like from the outside.

```bash
cd ~/Code/loom-oss
cp .env.homelab.example .env.homelab   # first time only — fill in the DSN
docker compose -f docker-compose.homelab.yml up -d --build
curl -s localhost:4444/health | jq .status

# Verify both networks survived the recreate (see incident below)
docker inspect loom-oss-loom-1 --format '{{json .NetworkSettings.Networks}}' | jq 'keys'
# expect: ["loom-oss_default", "n8n_n8n-network"] — if n8n_n8n-network is
# missing, Postgres is unreachable even though the container is "healthy":
docker network connect n8n_n8n-network loom-oss-loom-1 && docker restart loom-oss-loom-1
```

The gateway serves the observability API (`docs/observability-api.md`) on
:4444; the Nexus dashboard consumes it via `LOOM_API_URL`.

### Incident: 2026-09-07 network drop on rebuild → gateway-key auth failures

A `docker compose -f docker-compose.homelab.yml build && up -d` recreate cycle
dropped the container's `n8n_n8n-network` attachment even though the compose
file correctly declares both networks (`default` + external
`n8n_n8n-network`) — a known Compose gotcha where a full container recreate
doesn't always reliably reattach a secondary *external* network on the first
pass. The container came up "healthy" on its primary network but lost
Postgres connectivity, which surfaced as gateway-key/auth failures (401
"invalid gateway key") for anything routing through the gateway, including
`/compact` in this very session.

This was initially (incorrectly) attributed to the code in PR #87
(compression-observability instrumentation) and that PR was reverted as a
precaution. Post-incident review of the revert's diff confirmed it touched
only `app.py`, `logger.py`, `storage/{base,postgres,sqlite}.py`, and a test
file — no networking or Compose config — so the PR was not the cause. The
verification step above (added as the permanent fix) catches this class of
failure immediately after any rebuild instead of relying on a live auth
failure to surface it.

### Incident: 2026-09-15 rebuilt with the wrong compose file → "lost" gateway keys

After merging PR #95 (dashboard time-range filter UI, no backend/infra
changes), a rebuild was done with plain `docker compose build loom` /
`docker compose up -d loom` — no `-f docker-compose.homelab.yml` — to pick up
the new dashboard bundle. That command is valid, targets the *other*,
default `docker-compose.yml`, and completed without error: image built,
container recreated, health check green.

But the default file has no `n8n_n8n-network` entry at all, so the recreated
container landed on `loom-oss_default` only, with zero route to
`postgres-unified`. Symptoms looked like data loss: the dashboard's Settings
page showed no gateway keys on both the LAN IP and the public domain, and
`GET config/gateway-keys` returned 503. Nothing was actually deleted —
Postgres was simply unreachable, same failure family as the 2026-09-07
incident, but caused by the wrong compose file rather than a flaky reattach
of the right one.

Fix: `docker compose -f docker-compose.homelab.yml up -d --build`, then the
network-verification step above confirmed both `loom-oss_default` and
`n8n_n8n-network` were attached. Health check came back with
`auth_enabled: true` and the real session/metrics history, confirming the
gateway was reading the live `loom` database again.

Takeaway: rebuilding *this* deployment for *any* reason — including a
frontend-only change — always means the homelab overlay, never the bare
`docker compose` command. The plain `docker-compose.yml` only exists for the
public bundled-Postgres/`setup.sh` path (see "Reproducing on a new machine"
below) and should not be invoked against this host at all.

## External access: `loom.<your-domain>`

The gateway is reached from the LAN at `https://loom.<your-domain>`, proxied by
the homelab Caddy container on `<caddy-host-lan-ip>`. Caddy's config lives *outside*
this repo, in a separate git repo:
`<caddy-config-repo>/Caddyfile`. The site block is:

```caddyfile
# Loom Gateway - LAN-only, has built-in API auth
loom.<your-domain> {
	import security_filters
	reverse_proxy loom-oss-loom-1:4444
}
```

Two properties of this setup are worth knowing before debugging it:

**Caddy reaches Loom over `n8n_n8n-network`.** That is the only network the two
containers share (Caddy sits on several other networks of its own; Loom is on
`loom-oss_default` and `n8n_n8n-network`). So the external-network attachment that the incidents above
care about for *Postgres* is also what makes the *reverse proxy* work — a
dropped `n8n_n8n-network` breaks `loom.<your-domain>` with a 502 at the same
time it breaks the database, from a container that still reports "healthy".
The network-verification step in "Deploy / update" covers both.

**The deployment is LAN-only, deliberately.** `*.<your-domain>` is a DNS-only
wildcard record pointing at `<caddy-host-lan-ip>` — a private RFC1918 address, so
public resolvers hand out an address that is unroutable from the internet
(verify with a public resolver such as `1.1.1.1`). No tunnel or public ingress
covers these hostnames. The site block therefore has no forward-auth layer —
Loom carries its own auth, and its API still answers 401 unauthenticated
through the proxy.

### Incident: 2026-09-21 `loom.<your-domain>` TLS failure → missing Caddy site block

`https://loom.<your-domain>` failed at the TLS handshake with
`tlsv1 alert internal error` (curl exit 35), while `http://<caddy-host-lan-ip>:4444`
served normally. The gateway itself was never involved: container healthy,
HTTP 200 on 4444, and Caddy reachable on :443 for all its other hostnames.

The cause was simply that the Caddyfile had no site block for
`loom.<your-domain>` — 32 other services had one, this one did not. With no
vhost matching the SNI and no on-demand TLS configured, Caddy aborts the
handshake rather than serving a default certificate, which is why the failure
looked like a certificate problem rather than a missing-route problem. A valid
Let's Encrypt certificate for the hostname was already sitting in Caddy's store
(`/data/caddy/certificates/.../loom.<your-domain>/`, issued 2026-07-25), so the
block had existed at some earlier point and was lost — most likely during an
edit of the 1000-line Caddyfile. Nothing in this repo caused or could have
prevented it.

**The part that wastes time: the Caddyfile is a single-file bind mount.**
Editing it with a tool that writes atomically (temp file + rename) replaces the
file's inode, and a Docker *file* bind mount follows the inode, not the path —
so the container keeps reading the old file. Both `caddy validate` and
`caddy reload` then report success against the **stale** config, with no
warning that they are not seeing the edit. After the first edit-and-reload
cycle the site was still broken and still absent from the running config.
Confirm with:

```bash
CF=<caddy-config-repo>/Caddyfile
diff "$CF" <(docker exec caddy sh -c 'cat /etc/caddy/Caddyfile') && echo "in sync"
```

The content diff is the authoritative check. An inode comparison
(`stat -c %i` on each side) explains *why* they diverged, but differing inodes
on their own are not a fault: once the host file has been rewritten, the two
inodes stay different for the life of the container even though the contents
match, which is the state this host is in now. Docker re-resolves the path on
container restart, so a later restart picks up the host file correctly.

Fix without restarting Caddy (a restart would re-resolve the bind and also
work, at the cost of a brief outage for every other site on the host) — write
the host file's contents *through* the container's own path, preserving the
bound inode, then validate and reload:

```bash
docker exec -i caddy sh -c 'cat > /etc/caddy/Caddyfile' < "$CF"
docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec -w /etc/caddy caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
diff "$CF" <(docker exec caddy sh -c 'cat /etc/caddy/Caddyfile') && echo "in sync"
```

After the reload, `https://loom.<your-domain>` returned 200 serving the Loom
Gateway page on a valid certificate, with the other sites unaffected.

Takeaway: when a `*.<your-domain>` hostname fails the TLS handshake outright
while the service answers fine on its LAN port, suspect a missing Caddy site
block before suspecting certificates — and always verify a Caddyfile edit
actually reached the container, because a successful `validate`/`reload` pair
does not prove it did.

## Reproducing on a new machine

`loom.homelab.yaml` and `docker-compose.homelab.yml` are gitignored (not
shipped in the public repo — see the table above) — copy them in from a
private backup of this machine before running the steps below. Then create
`.env.homelab` with a DSN pointing at your Postgres (tables are created
automatically on first connect), and run the compose command above. If there
is no external Postgres, use `./setup.sh` instead and pick the
bundled-Postgres option (that path uses the default `docker-compose.yml`, not
the homelab overlay).

## Why Loom has its own database (`loom` on postgres-unified)

postgres-unified hosts one database per service (`n8n`, `pulse`,
`monday_sync`, `voice_jobs`, `google_token_vault`, …). Loom originally landed
in `pgvector_db` — a shared grab-bag database holding pgvector embeddings,
n8n chat histories, Alfred memories, and the legacy proxy's tables. The
dedicated `loom` database was created during the 2026-07-09 incident fix
(commit `e035d54`) and is the canonical target because:

1. **Loom's migration system assumes it owns the database.** It keeps a
   `schema_version` table and auto-applies versioned migrations on startup.
   While mispointed at `pgvector_db` (Aug–Sep 2026), it migrated that shared
   database's schema to v13 — mutating a schema other services sit on.
2. **Generic table names collide.** `metrics`, `sessions`, `requests`,
   `gateway_keys`, `schema_version` are exactly the names another service
   would also pick.
3. **AGE graph objects.** The variant store creates an Apache AGE graph and
   extension schemas; keeping those out of shared databases limits blast
   radius.
4. **Per-service backup/retention.** `pg_dump loom` captures exactly Loom's
   state; restore or retention policy changes can't touch other tenants.

`pgvector_db` still contains stale pre-2026-09-06 copies of Loom tables from
the split-brain periods — they are historical residue, not live data. The
legacy internal proxy (:8711) keeps its own separate tables and is unrelated
to the `loom` database.

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
