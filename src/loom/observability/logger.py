"""Fire-and-forget JSONL audit + metrics logging.

Both :meth:`AuditLogger.log_request` and :meth:`AuditLogger.log_metrics` append a single
JSON line to their respective files and never raise — logging failures must not take down
the gateway request path.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLogger:
    def __init__(
        self,
        audit_path: str = "logs/audit.jsonl",
        metrics_path: str = "logs/metrics.jsonl",
        content_path: str = "logs/content.jsonl",
        scanner: Any = None,
    ) -> None:
        self.audit_path = audit_path
        self.metrics_path = metrics_path
        self.content_path = content_path
        self._scanner = scanner
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for path in (self.audit_path, self.metrics_path, self.content_path):
            parent = os.path.dirname(path)
            if parent:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError:
                    pass

    def _write(self, path: str, record: dict[str, Any]) -> None:
        try:
            if self._scanner is not None:
                record = self._scanner.sanitize_log_entry(record)
            line = json.dumps(record, default=str)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
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
        ratelimit: Optional[dict] = None,
    ) -> None:
        record = {
            "ts": _utc_iso(),
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
        if ratelimit:
            record["ratelimit"] = ratelimit
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
            "ts": _utc_iso(),
            "request_id": request_id,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "cost_estimate": cost_estimate,
        }
        self._write(self.metrics_path, record)

    def log_content(
        self,
        request_id: str,
        model: str,
        source: str,
        provider: str,
        messages: Optional[list] = None,
        response_text: Optional[str] = None,
        content_logging: str = "off",
        response_content: Optional[list] = None,
        usage: Optional[dict] = None,
        stop_reason: Optional[str] = None,
        response_model: Optional[str] = None,
    ) -> None:
        """Log prompt/response content according to the content_logging level.

        Levels:
          off       — no-op
          metadata  — no-op (metadata already in audit log)
          after_scan — content after DLP scanner scrubs it
          full      — original content (sanitized if scanner active)
        """
        if content_logging in ("off", "metadata") or not messages:
            return

        record: dict[str, Any] = {
            "ts": _utc_iso(),
            "request_id": request_id,
            "model": model,
            "source": source,
            "provider": provider,
            "content_logging": content_logging,
        }

        if content_logging == "after_scan" and self._scanner is not None:
            scanned_messages = []
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    scanned, _ = self._scanner.apply(content, source=source, provider=provider)
                    scanned_messages.append({**msg, "content": scanned})
                else:
                    scanned_messages.append(msg)
            record["messages"] = scanned_messages
            if response_text:
                scanned_resp, _ = self._scanner.apply(response_text, source=source, provider=provider)
                record["response"] = scanned_resp
        elif content_logging == "full":
            if self._scanner is not None:
                record["messages"] = self._scanner.sanitize_log_entry({"m": messages})["m"]
                if response_text:
                    record["response"] = self._scanner.sanitize_log_entry({"r": response_text})["r"]
            else:
                record["messages"] = messages
                if response_text:
                    record["response"] = response_text
        else:
            return

        record["message_count"] = len(messages)
        if response_content:
            record["response_content"] = response_content
        if usage:
            record["usage"] = usage
        if stop_reason:
            record["stop_reason"] = stop_reason
        if response_model:
            record["response_model"] = response_model
        self._write(self.content_path, record)
