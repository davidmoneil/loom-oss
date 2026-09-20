"""Shadow-mode integration of the laya ML classifier for model-tier routing.

``laya`` (convaiinnovations/laya) is an ML prompt classifier benchmarked
against Loom's rule-based :class:`~loom.detection.engine.DetectionEngine`
(see ``laya-eval/bench.py`` in the internal eval harness — not part of this
repo). This module runs laya *in shadow*: alongside DetectionEngine, off the
request's critical path, purely to log a side-by-side comparison. It never
changes the tier returned to a caller of ``/v1/detect``.

Disabled by default (``config.laya_shadow.enabled``). ``laya`` is an optional
dependency (``pip install laya``, or the ``loom-gateway[laya]`` extra) —
its absence, or any failure in it, never breaks detection.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Mirrors laya-eval/bench.py's LAYA_QUESTIONS, kept in sync so shadow-mode
# comparisons stay apples-to-apples with the offline eval harness.
LAYA_QUESTIONS: dict[str, Any] = {
    "tier": {
        "type": "choice",
        "instructions": (
            "Which language-model tier should handle this request? Choose "
            "the cheapest tier that can answer it correctly."
        ),
        "criteria": {
            "economy": "trivial lookup, formatting, translation, or a one-line factual answer; no reasoning needed",
            "standard": "ordinary coding, writing, explanation or review; moderate effort but a single clear approach",
            "premium": "multi-step reasoning, architecture, tradeoff analysis, proofs, or long-horizon planning",
        },
    },
    "needs_reasoning": {
        "type": "noul",
        "instructions": "Does answering this request require extended step-by-step reasoning?",
    },
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LayaShadowRunner:
    """Runs laya predictions off the critical path and logs them for comparison.

    Model load and inference both happen on a single-worker background
    thread, so a slow or failed load can never block the asyncio event loop
    or add latency to the caller of ``/v1/detect``. :meth:`shadow` is
    fire-and-forget: it schedules the work and returns immediately.
    """

    def __init__(
        self,
        model_id: str = "convaiinnovations/laya",
        device: str = "cpu",
        sample_rate: float = 1.0,
        log_path: str = "logs/laya_shadow.jsonl",
    ) -> None:
        self.model_id = model_id
        self.device = device
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        self.log_path = log_path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya-shadow")
        self._agent: Any = None
        self._load_failed = False
        self._lock = threading.Lock()
        self._ensure_log_dir()

    def _ensure_log_dir(self) -> None:
        parent = os.path.dirname(self.log_path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                pass

    def _get_agent(self) -> Any:
        # Lazy-load on first use, on the background thread — never on the
        # asyncio loop. A load failure is sticky: we don't retry every call.
        if self._agent is not None or self._load_failed:
            return self._agent
        with self._lock:
            if self._agent is not None or self._load_failed:
                return self._agent
            try:
                import laya  # optional dependency

                self._agent = laya.load(self.model_id, device=self.device)
            except Exception:
                logger.warning(
                    "laya shadow: failed to load %r; disabling shadow mode for this process",
                    self.model_id,
                    exc_info=True,
                )
                self._load_failed = True
        return self._agent

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
        agent = self._get_agent()
        if agent is None:
            return
        t0 = time.time()
        try:
            res = agent.predict({"request": prompt}, LAYA_QUESTIONS)
            answers = res["answers"]
            laya_tier = answers["tier"]["choice"]
            laya_probabilities = answers["tier"].get("probabilities")
            needs_reasoning = answers.get("needs_reasoning", {}).get("noul")
        except Exception:
            logger.warning("laya shadow: prediction failed", exc_info=True)
            return
        latency_ms = (time.time() - t0) * 1000

        record = {
            "ts": _utc_iso(),
            "request_id": request_id,
            "source": source,
            "rule_tier": rule_tier,
            "rule_confidence": rule_confidence,
            "laya_tier": laya_tier,
            "laya_probabilities": laya_probabilities,
            "laya_needs_reasoning": needs_reasoning,
            "agree": (laya_tier == rule_tier) if rule_tier is not None else None,
            "laya_latency_ms": round(latency_ms, 1),
        }
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            logger.debug("laya shadow: failed to write log entry", exc_info=True)

    def close(self) -> None:
        """Stop accepting new work. Does not cancel in-flight predictions."""
        self._executor.shutdown(wait=False, cancel_futures=True)
