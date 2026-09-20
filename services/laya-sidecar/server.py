"""laya sidecar service.

Standalone process that loads the laya (convaiinnovations/laya) prompt
classifier and serves predictions over HTTP. Runs in its own container,
alongside llama-swap — never inside the loom-oss gateway process. laya
pulls a multi-GB torch/CUDA dependency set and ~2.4 GB RSS per resident
checkpoint; tying that to every gateway restart is exactly what this
sidecar avoids.

The service holds no routing policy: callers supply their own ``state``
and ``questions`` and get back whatever ``agent.predict()`` returns. See
loom-oss's ``src/loom/detection/laya_shadow.py`` for the one caller that
exists today (shadow-mode tier comparison, off by default, never used to
change routing).

Endpoints:
    POST /classify  {state, questions, checkpoint?} -> {answers, checkpoint, latency_ms}
    GET  /health     -> status, which checkpoints are resident

Run directly for local development:
    uvicorn server:app --host 0.0.0.0 --port 8091

Or via the Dockerfile in this directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LAYA_SIDECAR_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("laya_sidecar")

# Checkpoint name -> laya.load(..., subfolder=...) argument. "base" is the
# root checkpoint (subfolder=None); other entries are subfolders of the
# convaiinnovations/laya repo. Kept in sync with laya-eval/correct_usage.py.
CHECKPOINTS: dict[str, Optional[str]] = {
    "base": None,
    "typed-decisions": "typed-decisions",
}

MODEL_ID = os.environ.get("LAYA_SIDECAR_MODEL_ID", "convaiinnovations/laya")
DEVICE = os.environ.get("LAYA_SIDECAR_DEVICE", "cpu")
DEFAULT_CHECKPOINT = os.environ.get("LAYA_SIDECAR_DEFAULT_CHECKPOINT", "base")
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("LAYA_SIDECAR_REQUEST_TIMEOUT_SECONDS", "10"))
# Load every checkpoint at startup rather than lazily on first request, so
# the first caller doesn't pay a ~36s cold-load penalty and /health can
# report readiness truthfully. Set to "0" during development to skip the
# (slow, weight-downloading) load entirely.
PRELOAD = os.environ.get("LAYA_SIDECAR_PRELOAD", "1") not in ("0", "false", "False", "")

if DEFAULT_CHECKPOINT not in CHECKPOINTS:
    raise RuntimeError(
        f"LAYA_SIDECAR_DEFAULT_CHECKPOINT={DEFAULT_CHECKPOINT!r} is not one of {sorted(CHECKPOINTS)}"
    )

_agents: dict[str, Any] = {}
_agents_lock = threading.Lock()
_load_errors: dict[str, str] = {}


def _load_checkpoint(name: str) -> Any:
    """Load (and cache) one checkpoint's agent. Raises on failure."""
    if name not in CHECKPOINTS:
        raise KeyError(f"unknown checkpoint {name!r}; choices: {sorted(CHECKPOINTS)}")
    with _agents_lock:
        if name in _agents:
            return _agents[name]
        import laya  # heavy import (torch/CUDA) — deliberately deferred

        t0 = time.time()
        agent = laya.load(MODEL_ID, device=DEVICE, subfolder=CHECKPOINTS[name])
        logger.info("loaded checkpoint %r in %.1fs", name, time.time() - t0)
        _agents[name] = agent
        _load_errors.pop(name, None)
        return agent


def _get_checkpoint(name: str) -> Any:
    if name in _agents:
        return _agents[name]
    try:
        return _load_checkpoint(name)
    except KeyError:
        raise
    except Exception as exc:
        _load_errors[name] = str(exc)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    if PRELOAD:
        for name in CHECKPOINTS:
            try:
                _load_checkpoint(name)
            except Exception:
                logger.exception("failed to preload checkpoint %r", name)
    else:
        logger.info("LAYA_SIDECAR_PRELOAD=0: checkpoints load lazily on first request")
    yield


app = FastAPI(title="laya-sidecar", lifespan=lifespan)


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    logger.info(
        "%s %s -> %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        (time.time() - t0) * 1000,
    )
    return response


class ClassifyRequest(BaseModel):
    # Passed straight through to laya's agent.predict(state, questions).
    # This service holds no routing policy of its own.
    state: dict[str, Any]
    questions: dict[str, Any]
    checkpoint: str = Field(default=DEFAULT_CHECKPOINT)


class ClassifyResponse(BaseModel):
    answers: dict[str, Any]
    checkpoint: str
    latency_ms: float


@app.post("/classify", response_model=ClassifyResponse)
async def classify(req: ClassifyRequest) -> ClassifyResponse:
    if not req.state:
        raise HTTPException(status_code=400, detail="state must be non-empty")
    if not req.questions:
        raise HTTPException(status_code=400, detail="questions must be non-empty")
    try:
        agent = _get_checkpoint(req.checkpoint)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"checkpoint {req.checkpoint!r} unavailable: {exc}"
        ) from exc

    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(agent.predict, req.state, req.questions),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="prediction timed out") from exc
    except Exception as exc:
        logger.warning("prediction failed", exc_info=True)
        raise HTTPException(status_code=500, detail=f"prediction failed: {exc}") from exc
    latency_ms = (time.time() - t0) * 1000

    return ClassifyResponse(
        answers=result.get("answers", {}),
        checkpoint=req.checkpoint,
        latency_ms=round(latency_ms, 1),
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    loaded = sorted(_agents)
    return {
        "status": "healthy" if loaded else ("degraded" if _load_errors else "starting"),
        "model_id": MODEL_ID,
        "device": DEVICE,
        "default_checkpoint": DEFAULT_CHECKPOINT,
        "checkpoints_available": sorted(CHECKPOINTS),
        "checkpoints_loaded": loaded,
        "checkpoint_errors": dict(_load_errors),
    }
