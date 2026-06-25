"""Fire-and-forget JSONL audit + metrics logging.

Both :meth:`AuditLogger.log_request` and :meth:`AuditLogger.log_metrics` append a single
JSON line to their respective files and never raise — logging failures must not take down
the gateway request path.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional


class AuditLogger:
    def __init__(
        self,
        audit_path: str = "logs/audit.jsonl",
        metrics_path: str = "logs/metrics.jsonl",
    ) -> None:
        self.audit_path = audit_path
        self.metrics_path = metrics_path
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for path in (self.audit_path, self.metrics_path):
            parent = os.path.dirname(path)
            if parent:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError:
                    pass

    def _write(self, path: str, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(record, default=str)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            # Logging must never break the request path.
            pass

    def log_request(
        self,
        request_id: str,
        method: str,
        path: str,
        source: Optional[str] = None,
        model: Optional[str] = None,
        requested_model: Optional[str] = None,
        provider: Optional[str] = None,
        task_type: Optional[str] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        cost_estimate: float = 0.0,
        compressed: bool = False,
        compression_ratio: float = 1.0,
        routing_reason: Optional[str] = None,
        status_code: int = 200,
    ) -> None:
        record = {
            "ts": time.time(),
            "request_id": request_id,
            "method": method,
            "path": path,
            "source": source,
            "model": model,
            "requested_model": requested_model,
            "provider": provider,
            "task_type": task_type,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "cost_estimate": cost_estimate,
            "compressed": compressed,
            "compression_ratio": compression_ratio,
            "routing_reason": routing_reason,
            "status_code": status_code,
        }
        self._write(self.audit_path, record)

    def log_metrics(
        self,
        request_id: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        cost_estimate: float,
    ) -> None:
        record = {
            "ts": time.time(),
            "request_id": request_id,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "cost_estimate": cost_estimate,
        }
        self._write(self.metrics_path, record)
