"""Shadow-mode integration of the laya ML classifier for model-tier routing.

``laya`` (convaiinnovations/laya) is an ML prompt classifier benchmarked
against Loom's rule-based :class:`~loom.detection.engine.DetectionEngine`
(see ``laya-eval/bench.py`` in the internal eval harness — not part of this
repo). This module runs laya *in shadow*: alongside DetectionEngine, off the
request's critical path, purely to log a side-by-side comparison. It never
changes the tier returned to a caller of ``/v1/detect`` or ``/v1/chat/completions``.

Disabled by default (``config.laya_shadow.enabled``). Unlike an earlier
version of this module, laya is never imported or loaded in this process:
it runs as a standalone sidecar service (``services/laya-sidecar/`` in this
repo), its own container alongside llama-swap, and this module talks to it
over plain HTTP. A multi-GB torch/CUDA model tied to every gateway restart
is exactly what the sidecar avoids — see the implementation plan for the
full reasoning. The sidecar being down, slow, or never having been started
is indistinguishable from "shadow mode found nothing interesting": every
failure mode here is silent and never adds request latency.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from loom.netcheck import UnsafeURLError, validate_outbound_url

logger = logging.getLogger(__name__)

# Bump when LAYA_QUESTIONS changes so log rows from different question sets
# are never compared as one series.
#   1: tier choice + needs_reasoning noul (noul separated 7/12 on a probe)
#   2: needs_reasoning -> reasoning_depth score, cut at REASONING_CUTOFF
LAYA_QUESTION_VERSION = 2

# Score at or above this means "needs extended reasoning". The 2026-10-12
# probe (12 crafted asks) separated 12/12 with a gap between 1.22 and 1.76.
REASONING_CUTOFF = 1.5

# Question design rules (score + cutoff over noul, field names in backticks):
# AIProjects .claude/context/patterns/laya-question-design.md
LAYA_QUESTIONS: dict[str, Any] = {
    "tier": {
        "type": "choice",
        "instructions": (
            "Which language-model tier should handle the request in `request`? "
            "Choose the cheapest tier that can answer it correctly."
        ),
        "criteria": {
            "economy": "trivial lookup, formatting, translation, or a one-line factual answer; no reasoning needed",
            "standard": "ordinary coding, writing, explanation or review; moderate effort but a single clear approach",
            "premium": "multi-step reasoning, architecture, tradeoff analysis, proofs, or long-horizon planning",
        },
    },
    "reasoning_depth": {
        "type": "score",
        "instructions": "How much step-by-step reasoning does answering `request` need?",
        "criteria": [
            "none: a lookup, one-liner or mechanical edit",
            "a little: a short explanation or a single clear fix",
            "a lot: several dependent steps, debugging or comparing options",
            "deep: design, proof, root-cause analysis or long-horizon planning",
        ],
    },
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:16]


class LayaShadowClient:
    """Calls the laya sidecar off the critical path and logs results for comparison.

    Every network call happens on a background thread pool, so a slow or
    unreachable sidecar can never block the asyncio event loop or add
    latency to the caller. :meth:`shadow` is fire-and-forget: it schedules
    the work and returns immediately.
    """

    def __init__(
        self,
        url: str = "http://localhost:8091",
        checkpoint: str = "base",
        timeout_seconds: float = 2.0,
        allow_private_url: bool = False,
        sample_rate: float = 1.0,
        max_prompt_chars: int = 4000,
        cache_size: int = 512,
        log_path: str = "logs/laya_shadow.jsonl",
        sources: Optional[list[str]] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.checkpoint = checkpoint
        self.timeout_seconds = timeout_seconds
        self.allow_private_url = allow_private_url
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        # None = shadow every source; otherwise only listed sources (the
        # gateway filters on this before calling shadow()). Live-editable.
        self.sources = list(sources) if sources is not None else None
        # laya's benchmarked value is specifically on short prompts; long
        # prompts already escalate correctly on length alone (see
        # DetectionEngine.LONG_PROMPT_TOKENS), so skip them here rather than
        # spend a sidecar call on a comparison that isn't interesting.
        self.max_prompt_chars = max_prompt_chars
        self.log_path = log_path
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="laya-shadow")
        self._client = httpx.Client(timeout=timeout_seconds)
        self._cache_size = max(0, cache_size)
        self._cache: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self._ensure_log_dir()

    def _ensure_log_dir(self) -> None:
        parent = os.path.dirname(self.log_path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                pass

    def _cache_get(self, key: str) -> Optional[dict[str, Any]]:
        if self._cache_size == 0:
            return None
        with self._cache_lock:
            value = self._cache.get(key)
            if value is not None:
                self._cache.move_to_end(key)
            return value

    def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        if self._cache_size == 0:
            return
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def shadow(
        self,
        request_id: str,
        source: str,
        prompt: str,
        rule_tier: Optional[str],
        rule_confidence: Optional[float],
    ) -> None:
        """Fire-and-forget: schedule a laya prediction for comparison logging.

        Never raises and never blocks the caller — worst case is a skipped
        or delayed shadow log entry.
        """
        if not prompt or len(prompt) > self.max_prompt_chars:
            return
        if self.sample_rate < 1.0 and random.random() > self.sample_rate:
            return
        try:
            self._executor.submit(
                self._run, request_id, source, prompt, rule_tier, rule_confidence
            )
        except Exception:
            # Executor already shut down, etc. — never affect the caller.
            logger.debug("laya shadow: failed to schedule prediction", exc_info=True)

    def _run(
        self,
        request_id: str,
        source: str,
        prompt: str,
        rule_tier: Optional[str],
        rule_confidence: Optional[float],
    ) -> None:
        prompt_hash = _prompt_hash(prompt)

        cached = self._cache_get(prompt_hash)
        if cached is not None:
            self._write(
                self._record(
                    request_id, source, prompt_hash, rule_tier, rule_confidence,
                    cached["laya_tier"], cached["laya_probabilities"],
                    cached["laya_reasoning_score"], latency_ms=0.0, cached=True,
                )
            )
            return

        try:
            validate_outbound_url(self.url, allow_private=self.allow_private_url)
        except UnsafeURLError:
            logger.warning("laya shadow: rejecting unsafe sidecar url %r", self.url, exc_info=True)
            return

        t0 = time.time()
        try:
            resp = self._client.post(
                f"{self.url}/classify",
                json={
                    "state": {"request": prompt},
                    "questions": LAYA_QUESTIONS,
                    "checkpoint": self.checkpoint,
                },
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            answers = resp.json()["answers"]
            laya_tier = answers["tier"]["choice"]
            laya_probabilities = answers["tier"].get("probabilities")
            reasoning_score = answers.get("reasoning_depth", {}).get("score")
        except Exception:
            # Unreachable, slow, or malformed sidecar response — this must
            # never surface to the request path. Debug, not warning: an
            # unstarted sidecar is the expected common case (off by default).
            logger.debug("laya shadow: sidecar call failed", exc_info=True)
            return
        latency_ms = (time.time() - t0) * 1000

        self._cache_put(
            prompt_hash,
            {
                "laya_tier": laya_tier,
                "laya_probabilities": laya_probabilities,
                "laya_reasoning_score": reasoning_score,
            },
        )
        self._write(
            self._record(
                request_id, source, prompt_hash, rule_tier, rule_confidence,
                laya_tier, laya_probabilities, reasoning_score,
                latency_ms=round(latency_ms, 1), cached=False,
            )
        )

    def _record(
        self,
        request_id: str,
        source: str,
        prompt_hash: str,
        rule_tier: Optional[str],
        rule_confidence: Optional[float],
        laya_tier: Optional[str],
        laya_probabilities: Any,
        laya_reasoning_score: Optional[float],
        latency_ms: float,
        cached: bool,
    ) -> dict[str, Any]:
        return {
            "ts": _utc_iso(),
            "request_id": request_id,
            "source": source,
            "prompt_hash": prompt_hash,
            "question_version": LAYA_QUESTION_VERSION,
            "rule_tier": rule_tier,
            "rule_confidence": rule_confidence,
            "laya_tier": laya_tier,
            "laya_probabilities": laya_probabilities,
            "laya_reasoning_score": laya_reasoning_score,
            "laya_needs_reasoning": (
                laya_reasoning_score >= REASONING_CUTOFF
                if isinstance(laya_reasoning_score, (int, float)) else None
            ),
            "agree": (laya_tier == rule_tier) if rule_tier is not None else None,
            "laya_latency_ms": latency_ms,
            "cached": cached,
        }

    def _write(self, record: dict[str, Any]) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            logger.debug("laya shadow: failed to write log entry", exc_info=True)

    def close(self) -> None:
        """Stop accepting new work. Does not cancel in-flight predictions."""
        self._executor.shutdown(wait=False, cancel_futures=True)
        try:
            self._client.close()
        except Exception:
            pass
