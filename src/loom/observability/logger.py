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
        max_bytes: int = 0,
        backup_count: int = 3,
        content_max_bytes: Optional[int] = None,
        content_backup_count: Optional[int] = None,
    ) -> None:
        self.audit_path = audit_path
        self.metrics_path = metrics_path
        self.content_path = content_path
        self._scanner = scanner
        # max_bytes <= 0 disables rotation (legacy unbounded behaviour).
        self._max_bytes = max_bytes
        self._backup_count = max(backup_count, 0)
        # content.jsonl carries full prompt bodies and grows orders of magnitude
        # faster than audit/metrics, so it gets its own limits (None = shared).
        self._content_max_bytes = max_bytes if content_max_bytes is None else content_max_bytes
        self._content_backup_count = max(
            self._backup_count if content_backup_count is None else content_backup_count, 0
        )
        self._ensure_dirs()

    def _limits(self, path: str) -> tuple[int, int]:
        if path == self.content_path:
            return self._content_max_bytes, self._content_backup_count
        return self._max_bytes, self._backup_count

    def content_paths(self) -> list[str]:
        """The live content file plus existing rotated backups, newest first."""
        _, backups = self._limits(self.content_path)
        paths = [self.content_path] + [f"{self.content_path}.{i}" for i in range(1, backups + 1)]
        return [p for p in paths if os.path.exists(p)]

    def _rotate_if_needed(self, path: str) -> None:
        """Roll ``path`` -> ``path.1`` -> ... once it exceeds its size limit.

        The oldest backup is dropped, so disk use per file is bounded by
        roughly ``max_bytes * (backup_count + 1)``. Never raises: a failed
        rotation must not cost the log line itself.
        """
        max_bytes, backups = self._limits(path)
        if max_bytes <= 0:
            return
        try:
            if os.path.getsize(path) < max_bytes:
                return
            if backups == 0:
                os.remove(path)
                return
            for i in range(backups, 1, -1):
                src = f"{path}.{i - 1}"
                if os.path.exists(src):
                    os.replace(src, f"{path}.{i}")
            os.replace(path, f"{path}.1")
        except OSError:
            pass

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
            self._rotate_if_needed(path)
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
        session_id: Optional[str] = None,
        skip_reasons: Optional[str] = None,
        upstream_request_id: Optional[str] = None,
        client_app: Optional[str] = None,
        client_job: Optional[str] = None,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        auth_type: Optional[str] = None,
        tags: Optional[dict] = None,
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
            "session_id": session_id,
            # Join fields: Anthropic's own request id, the client app family
            # and the optional x-nexus-job tag ("" when absent).
            "upstream_request_id": upstream_request_id or "",
            "client_app": client_app or "",
            "client_job": client_job or "",
            # "oauth" (Claude Max) / "api_key" (console key) / None; label only.
            "auth_type": auth_type,
        }
        if cache_read_tokens or cache_creation_tokens:
            record["cache_read_tokens"] = cache_read_tokens
            record["cache_creation_tokens"] = cache_creation_tokens
        if tags:
            record["tags"] = tags
        if ratelimit:
            record["ratelimit"] = ratelimit
        if skip_reasons:
            record["skip_reasons"] = skip_reasons
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

    def _scan_system(self, system: Any, source: str, provider: str) -> Any:
        """Scan a system prompt: a string, or Anthropic text blocks."""
        if isinstance(system, str):
            return self._scanner.apply(system, source=source, provider=provider)[0]
        if isinstance(system, list):
            out = []
            for block in system:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    text, _ = self._scanner.apply(block["text"], source=source, provider=provider)
                    out.append({**block, "text": text})
                else:
                    out.append(block)
            return out
        return system

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
        system: Any = None,
        request_params: Optional[dict] = None,
    ) -> None:
        """Log prompt/response content according to the content_logging level.

        ``system`` (the request's system prompt, string or block list) is scanned
        like message text. ``request_params`` holds non-content settings (tool
        names, tool_choice, max_tokens, temperature, thinking) and is logged as is.

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
            if system:
                record["system"] = self._scan_system(system, source, provider)
        elif content_logging == "full":
            if self._scanner is not None:
                record["messages"] = self._scanner.sanitize_log_entry({"m": messages})["m"]
                if response_text:
                    record["response"] = self._scanner.sanitize_log_entry({"r": response_text})["r"]
                if system:
                    record["system"] = self._scanner.sanitize_log_entry({"s": system})["s"]
            else:
                record["messages"] = messages
                if response_text:
                    record["response"] = response_text
                if system:
                    record["system"] = system
        else:
            return

        if request_params:
            record["request_params"] = request_params
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
