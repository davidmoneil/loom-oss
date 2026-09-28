FROM python:3.12-slim AS base

# Security: run as non-root user
RUN groupadd -r loom && useradd -r -g loom -d /app -s /sbin/nologin loom

WORKDIR /app

ARG INSTALL_EXTRAS=""

# Copy source + install (single layer for correct package-data inclusion)
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY loom.example.yaml ./loom.example.yaml
RUN if [ -n "$INSTALL_EXTRAS" ]; then \
      pip install --no-cache-dir ".[$INSTALL_EXTRAS]"; \
    else \
      pip install --no-cache-dir .; \
    fi

# Fail the build, not the deploy, if the gateway entrypoint or the Postgres
# driver it optionally depends on can't import. Same failure class as
# pulse#4 (SQLAlchemy 2.1 broke a bare postgresql:// URL at container start
# on 2026-09-27) — here the risk is the `postgres` extra silently missing
# from the image, not a SQLAlchemy dialect (loom-oss talks to Postgres via
# psycopg directly, no SQLAlchemy in this repo). No network/DB/secrets
# needed: create_app() builds routes only, storage.connect() happens later
# in the app's lifespan handler, not at import time.
RUN python -c "import loom.gateway.app; print('build-start-check: gateway entrypoint imports cleanly')" && \
    if echo "$INSTALL_EXTRAS" | grep -q postgres; then \
      python -c "import psycopg; print('build-start-check: psycopg (postgres driver) imports cleanly')"; \
    fi

# Create data and log directories owned by loom user
RUN mkdir -p /app/data /app/logs && chown -R loom:loom /app

USER loom

EXPOSE 4444

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4444/health')" || exit 1

CMD ["uvicorn", "loom.gateway.app:app", "--host", "0.0.0.0", "--port", "4444"]
