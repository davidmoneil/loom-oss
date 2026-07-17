#!/usr/bin/env bash
# Loom gateway setup — generates loom.yaml + .env and (optionally) starts
# the Docker stack. Safe to re-run; existing files are never overwritten
# without confirmation.
set -euo pipefail

cd "$(dirname "$0")"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }

bold "Loom gateway setup"
echo

# ---------------------------------------------------------------- loom.yaml
if [[ -f loom.yaml ]]; then
    read -r -p "loom.yaml already exists — overwrite with the example config? [y/N] " ans
    if [[ "$ans" == [Yy] ]]; then
        cp loom.example.yaml loom.yaml
        info "loom.yaml reset from loom.example.yaml"
    else
        info "keeping existing loom.yaml"
    fi
else
    cp loom.example.yaml loom.yaml
    info "created loom.yaml from loom.example.yaml"
fi

# ------------------------------------------------------------------ storage
echo
bold "Storage backend"
info "1) SQLite (default — zero dependencies, data kept in a Docker volume)"
info "2) Existing PostgreSQL (you provide a DSN)"
info "3) Bundled PostgreSQL (docker compose starts one for you)"
read -r -p "Choose [1/2/3, default 1]: " choice
choice="${choice:-1}"

ENV_LINES=()
case "$choice" in
    2)
        read -r -p "Postgres DSN (postgresql://user:pass@host:5432/loom): " dsn
        while [[ -z "$dsn" ]]; do
            read -r -p "DSN cannot be empty: " dsn
        done
        ENV_LINES+=("LOOM_STORAGE_BACKEND=postgres")
        ENV_LINES+=("LOOM_POSTGRES_DSN=${dsn}")
        ENV_LINES+=("LOOM_INSTALL_EXTRAS=postgres")
        info "the Loom database user needs CREATE TABLE on the target database"
        ;;
    3)
        pg_password="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
        ENV_LINES+=("LOOM_STORAGE_BACKEND=postgres")
        ENV_LINES+=("LOOM_POSTGRES_DSN=postgresql://loom:${pg_password}@postgres:5432/loom")
        ENV_LINES+=("LOOM_PG_PASSWORD=${pg_password}")
        ENV_LINES+=("LOOM_INSTALL_EXTRAS=postgres")
        ENV_LINES+=("COMPOSE_PROFILES=postgres")
        info "generated a random password for the bundled Postgres (stored in .env)"
        ;;
    *)
        ENV_LINES+=("LOOM_STORAGE_BACKEND=sqlite")
        info "using SQLite at data/loom.db (inside the loom-data volume)"
        ;;
esac

# --------------------------------------------------------------------- .env
echo
if [[ -f .env ]]; then
    read -r -p ".env already exists — overwrite? [y/N] " ans
    if [[ "$ans" != [Yy] ]]; then
        bold ".env left untouched — add these lines yourself:"
        printf '  %s\n' "${ENV_LINES[@]}"
        exit 0
    fi
fi
printf '%s\n' "${ENV_LINES[@]}" > .env
chmod 600 .env
info "wrote .env"

# ------------------------------------------------------------------- launch
echo
read -r -p "Build and start the gateway now with docker compose? [Y/n] " ans
if [[ "$ans" == [Nn] ]]; then
    bold "Done. Start it later with: docker compose up -d --build"
    exit 0
fi

docker compose up -d --build

port="${LOOM_PORT:-4444}"
echo
bold "Waiting for the gateway to become healthy..."
for _ in $(seq 1 30); do
    if curl -sf -m 2 "http://localhost:${port}/health" > /dev/null 2>&1; then
        echo
        bold "Loom gateway is up:"
        info "health:  http://localhost:${port}/health"
        info "audit:   http://localhost:${port}/api/audit"
        info "costs:   http://localhost:${port}/api/costs"
        info "point clients at it with: export ANTHROPIC_BASE_URL=http://localhost:${port}"
        exit 0
    fi
    sleep 2
done

echo
bold "Gateway did not report healthy within 60s — check: docker compose logs loom"
exit 1
