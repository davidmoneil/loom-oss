# laya sidecar

Standalone HTTP service around the [laya](https://huggingface.co/convaiinnovations/laya)
prompt classifier (`convaiinnovations/laya`). Runs as its own container, alongside
llama-swap — never inside the loom-oss gateway process. laya is not a servable
model (it never generates text; it answers typed `choice`/`score`/`noul` questions
in one forward pass), and it pulls a multi-GB torch/CUDA dependency set, so it gets
its own process and its own restart lifecycle instead of the gateway's.

The service holds no routing policy: callers supply their own `state` and
`questions` and get back whatever `agent.predict()` returns. The one caller in
this repo today is loom-oss's shadow-mode detector comparison
(`src/loom/detection/laya_shadow.py`, off by default, never used to change
routing) — see the implementation plan referenced in that module's docstring.

## Run locally

```bash
cd services/laya-sidecar
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8091
```

First startup loads both resident checkpoints (`base` and `typed-decisions`),
measured at ~36s cold on CPU. Set `LAYA_SIDECAR_PRELOAD=0` to load lazily on
first `/classify` request instead (useful for quick local iteration when you
don't need both checkpoints warm).

## Run via Docker

```bash
docker build -t laya-sidecar services/laya-sidecar
docker run -p 8091:8091 laya-sidecar
```

Or via the `laya` profile in the repo's root `docker-compose.yml`:

```bash
COMPOSE_PROFILES=laya docker compose up laya-sidecar
```

## Endpoints

- `POST /classify` — `{"state": {...}, "questions": {...}, "checkpoint": "base"}`
  → `{"answers": {...}, "checkpoint": "base", "latency_ms": 41.2}`. `state` and
  `questions` are laya's own typed-question format; see
  `agent.predict(state, questions)` in the
  [laya usage examples](https://huggingface.co/convaiinnovations/laya). Returns
  400 for an unknown `checkpoint`, 503 if that checkpoint failed to load, 504 on
  a prediction that exceeds `LAYA_SIDECAR_REQUEST_TIMEOUT_SECONDS`.
- `GET /health` — status, model id, device, which checkpoints are resident and
  which failed to load.

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LAYA_SIDECAR_MODEL_ID` | `convaiinnovations/laya` | HF model id passed to `laya.load` |
| `LAYA_SIDECAR_DEVICE` | `cpu` | `cpu` or a CUDA device string |
| `LAYA_SIDECAR_DEFAULT_CHECKPOINT` | `base` | Checkpoint used when a request omits `checkpoint` |
| `LAYA_SIDECAR_REQUEST_TIMEOUT_SECONDS` | `10` | Per-request prediction timeout |
| `LAYA_SIDECAR_PRELOAD` | `1` | Load all checkpoints at startup vs. lazily on first use |
| `LAYA_SIDECAR_LOG_LEVEL` | `INFO` | Python logging level |

## Tests

```bash
pip install -r requirements.txt -r tests/requirements.txt
pytest tests/
```

Tests mock `laya.load`/`agent.predict` — real laya weights are never a test
dependency (they're a ~2.4GB-per-checkpoint download).
