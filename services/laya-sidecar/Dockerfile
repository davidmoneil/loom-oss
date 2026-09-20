FROM python:3.12-slim AS base

# Security: run as non-root user
RUN groupadd -r laya && useradd -r -g laya -d /app -s /sbin/nologin laya

WORKDIR /app

COPY requirements.txt .
# CPU-only by default: laya's checkpoints are ~421M params, small enough
# that a GPU isn't required. Set LAYA_SIDECAR_DEVICE=cuda and switch the
# base image / add a CUDA-enabled torch build if you want GPU inference.
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN chown -R laya:laya /app
USER laya

EXPOSE 8091

# start-period is generous: PRELOAD=1 (the default) loads both checkpoints
# at startup, measured at ~36s cold on CPU, before the first request lands.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8091/health')" || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8091"]
