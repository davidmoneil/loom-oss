"""Optional, off-by-default rate-limit sink.

Writes one row per upstream Anthropic response into a Postgres table whose
columns mirror the Anthropic rate-limit headers plus usage/cost/latency
metadata (default table name ``api_headers``). It stores headers and
metadata only - never prompt or response content, and never request headers.

Design constraints:
  * Disabled unless ``observability.ratelimit_sink.enabled`` is true. When
    disabled no thread is started and no database connection is attempted.
  * Never blocks the response path: ``emit`` only enqueues onto a bounded
    queue (dropping on overflow); a daemon thread does all database work.
  * Fails open: connection or insert errors are swallowed and logged at most
    once per minute.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

_log = logging.getLogger("loom.ratelimit_sink")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}(\.[A-Za-z_][A-Za-z0-9_]{0,62})?$")
_WARN_INTERVAL_S = 60.0

# Column order of the INSERT (``ts`` is left to the table default).
COLUMNS = (
    "request_id", "method", "path", "model", "status_code",
    "rl_requests_limit", "rl_requests_remaining", "rl_requests_reset",
    "rl_tokens_limit", "rl_tokens_remaining", "rl_tokens_reset",
    "rl_input_tokens_limit", "rl_input_tokens_remaining", "rl_input_tokens_reset",
    "rl_output_tokens_limit", "rl_output_tokens_remaining", "rl_output_tokens_reset",
    "retry_after", "source", "raw_headers",
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cost_usd", "rl_unified_status", "rl_unified_reset",
    "rl_unified_5h_util", "rl_unified_5h_status",
    "rl_unified_7d_util", "rl_unified_7d_status",
    "rl_unified_7d_model_util", "rl_unified_7d_model_name",
    "stop_reason", "response_ms",
)


def _int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _num(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _ts(val: Any) -> Optional[str]:
    """Return an RFC3339 string Postgres can cast to timestamptz, or None."""
    if not isinstance(val, str):
        return None
    try:
        datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
    return val


def build_row(
    *,
    source: str,
    method: str,
    path: str,
    status_code: int,
    model: Optional[str],
    ratelimit: Optional[dict],
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[float] = None,
    stop_reason: Optional[str] = None,
) -> dict:
    """Map a loom ratelimit snapshot + request metadata onto the table columns."""
    rl = ratelimit or {}
    raw = rl.get("raw_headers")
    row = {
        "request_id": rl.get("upstream_request_id"),
        "method": method,
        "path": path,
        "model": model,
        "status_code": status_code,
        "retry_after": _int(rl.get("retry_after")),
        "source": source,
        "raw_headers": json.dumps(raw, sort_keys=True) if raw else None,
        "input_tokens": _int(tokens_in),
        "output_tokens": _int(tokens_out),
        "cache_creation_tokens": _int(cache_creation_tokens),
        "cache_read_tokens": _int(cache_read_tokens),
        "cost_usd": round(cost_usd, 6) if isinstance(cost_usd, (int, float)) else None,
        "rl_unified_status": rl.get("ratelimit_unified_status"),
        "rl_unified_reset": _int(rl.get("ratelimit_unified_reset")),
        "rl_unified_5h_util": _num(rl.get("ratelimit_unified_5h_utilization")),
        "rl_unified_5h_status": rl.get("ratelimit_unified_5h_status"),
        "rl_unified_7d_util": _num(rl.get("ratelimit_unified_7d_utilization")),
        "rl_unified_7d_status": rl.get("ratelimit_unified_7d_status"),
        "rl_unified_7d_model_util": _num(rl.get("ratelimit_unified_7d_model_utilization")),
        "rl_unified_7d_model_name": rl.get("ratelimit_unified_7d_model_name"),
        "stop_reason": stop_reason,
        "response_ms": _int(latency_ms),
    }
    for bucket in ("requests", "tokens", "input_tokens", "output_tokens"):
        row[f"rl_{bucket}_limit"] = _int(rl.get(f"ratelimit_{bucket}_limit"))
        row[f"rl_{bucket}_remaining"] = _int(rl.get(f"ratelimit_{bucket}_remaining"))
        row[f"rl_{bucket}_reset"] = _ts(rl.get(f"ratelimit_{bucket}_reset"))
    return row


class RateLimitSink:
    """Background writer. ``emit`` never blocks, never raises."""

    def __init__(
        self,
        dsn: str,
        table: str = "api_headers",
        source: str = "loom-oss",
        queue_size: int = 1000,
        connect: Optional[Callable[[str], Any]] = None,
        start: bool = True,
    ) -> None:
        if not _IDENT_RE.match(table):
            raise ValueError(f"invalid ratelimit sink table name: {table!r}")
        self._dsn = dsn
        self.table = table
        self.source = source
        self._connect = connect or self._default_connect
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=max(1, queue_size))
        self._conn: Any = None
        self._last_warn = 0.0
        self._stop = threading.Event()
        self.dropped = 0
        self._sql = "INSERT INTO {} ({}) VALUES ({})".format(
            table, ", ".join(COLUMNS), ", ".join(
                f"%({c})s::jsonb" if c == "raw_headers" else f"%({c})s" for c in COLUMNS
            ),
        )
        self._thread: Optional[threading.Thread] = None
        if start:
            self._thread = threading.Thread(
                target=self._run, name="loom-ratelimit-sink", daemon=True,
            )
            self._thread.start()

    @staticmethod
    def _default_connect(dsn: str) -> Any:
        import psycopg  # optional dependency, only needed when the sink is on

        return psycopg.connect(dsn, autocommit=True, connect_timeout=5)

    def _warn(self, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last_warn >= _WARN_INTERVAL_S:
            self._last_warn = now
            try:
                _log.warning(msg, *args)
            except Exception:
                pass

    def emit(self, **fields: Any) -> None:
        """Queue one row. Accepts the keyword arguments of :func:`build_row`
        (minus ``source``, which comes from config)."""
        try:
            row = build_row(source=self.source, **fields)
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1
            self._warn("ratelimit sink queue full; dropping rows (dropped=%d)", self.dropped)
        except Exception as exc:
            self._warn("ratelimit sink could not build row: %s", type(exc).__name__)

    def _write(self, row: dict) -> None:
        if self._conn is None:
            self._conn = self._connect(self._dsn)
        cur = self._conn.cursor()
        try:
            cur.execute(self._sql, row)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                row = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if row is None:
                break
            self.flush_one(row)

    def flush_one(self, row: dict) -> bool:
        """Write one row; on any failure drop it, reset the connection, warn."""
        try:
            self._write(row)
            return True
        except Exception as exc:
            self._warn("ratelimit sink write failed (%s); row dropped", type(exc).__name__)
            conn, self._conn = self._conn, None
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            return False

    def close(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass


def build_sink(cfg: Any) -> Optional[RateLimitSink]:
    """Create a sink from ``ObservabilityConfig.ratelimit_sink``; None when
    disabled, misconfigured or the DSN env var is unset. Never raises."""
    import os

    try:
        if cfg is None or not cfg.enabled:
            return None
        dsn = os.environ.get(cfg.dsn_env, "")
        if not dsn:
            _log.warning("ratelimit sink enabled but env var %s is empty; sink off", cfg.dsn_env)
            return None
        return RateLimitSink(
            dsn=dsn, table=cfg.table, source=cfg.source, queue_size=cfg.queue_size,
        )
    except Exception as exc:
        _log.warning("ratelimit sink disabled: %s", type(exc).__name__)
        return None
