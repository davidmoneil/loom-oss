FROM python:3.14-slim AS base

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

# Create data and log directories owned by loom user
RUN mkdir -p /app/data /app/logs && chown -R loom:loom /app

USER loom

EXPOSE 4444

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4444/health')" || exit 1

CMD ["uvicorn", "loom.gateway.app:app", "--host", "0.0.0.0", "--port", "4444"]
