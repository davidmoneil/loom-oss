"""Configurable request tags and header-name discovery.

An operator lists which incoming request headers to capture and the tag name
each one becomes (``request_tags.headers`` in the config). Captured values are
stored with the request's audit and metrics rows, and can be filtered and
grouped in the API and dashboard.

Security rules (enforced here, not just in config validation):

* Credential-bearing headers are never captured or listed, even if configured:
  a fixed deny-list plus any header whose name contains ``key``, ``token``,
  ``secret`` or ``auth``.
* Header discovery records header *names* and counts only, never values.
* Tag values are length-limited, stripped of control characters and newlines,
  and stored as plain strings.

Nothing here raises into the request path.
"""

from __future__ import annotations

import contextvars
import re
import threading
import time
from collections import Counter, deque
from typing import Any, Mapping, Optional

DENIED_HEADERS = frozenset({
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "x-loom-gateway-key",
})
_DENIED_SUBSTRINGS = ("key", "token", "secret", "auth")

_TAG_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
_HEADER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")

# Tags of the request currently being handled; set by the ASGI middleware so
# _record_request needs no extra plumbing through every call site.
current_tags: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "loom_request_tags", default=None
)


def is_denied_header(name: str) -> bool:
    """True for any header that may carry a credential."""
    n = (name or "").strip().lower()
    if not n:
        return True
    return n in DENIED_HEADERS or any(s in n for s in _DENIED_SUBSTRINGS)


def valid_tag_name(name: str) -> bool:
    return bool(_TAG_NAME_RE.match(name or ""))


def sanitize_value(value: Any, max_len: int) -> str:
    """Plain string: control chars/newlines removed, trimmed, length-limited."""
    text = _CONTROL_RE.sub(" ", str(value)).strip()
    return text[: max(int(max_len), 0)]


def build_header_map(headers: Mapping[str, str]) -> dict[str, str]:
    """Normalize a configured ``{header: tag}`` mapping: drop denied headers
    and invalid tag names. Returns lower-cased header names."""
    out: dict[str, str] = {}
    for header, tag in (headers or {}).items():
        h = str(header).strip().lower()
        t = str(tag).strip().lower()
        if is_denied_header(h) or not valid_tag_name(t):
            continue
        out[h] = t
    return out


def extract_tags(headers: Any, cfg: Any) -> dict[str, str]:
    """Return ``{tag_name: value}`` for the configured headers present on the
    request. ``{}`` when disabled. Never raises."""
    try:
        if cfg is None or not cfg.enabled:
            return {}
        tags: dict[str, str] = {}
        max_tags = max(int(cfg.max_tags), 0)
        for header, tag in build_header_map(cfg.headers).items():
            if len(tags) >= max_tags:
                break
            raw = headers.get(header)
            if raw is None:
                continue
            value = sanitize_value(raw, cfg.max_value_length)
            if value:
                tags[tag] = value
        return tags
    except Exception:
        return {}


class HeaderObserver:
    """In-memory, per-process counter of incoming header *names* (never values),
    kept in hourly buckets so a window can be queried. Lost on restart."""

    def __init__(self, max_names: int = 256, max_hours: int = 24 * 7) -> None:
        self._lock = threading.Lock()
        self._buckets: deque = deque()  # (hour_epoch, Counter)
        self._max_names = max_names
        self._max_hours = max_hours

    def observe(self, headers: Any, now: Optional[float] = None) -> None:
        try:
            names = {
                n for n in (str(k).strip().lower() for k in headers.keys())
                if _HEADER_NAME_RE.match(n) and not is_denied_header(n)
            }
            if not names:
                return
            hour = int((now if now is not None else time.time()) // 3600)
            with self._lock:
                if not self._buckets or self._buckets[-1][0] != hour:
                    self._buckets.append((hour, Counter()))
                    while len(self._buckets) > self._max_hours:
                        self._buckets.popleft()
                counter = self._buckets[-1][1]
                for n in names:
                    if n in counter or len(counter) < self._max_names:
                        counter[n] += 1
        except Exception:
            pass

    def snapshot(self, hours: int = 24, now: Optional[float] = None) -> list[dict]:
        since = int((now if now is not None else time.time()) // 3600) - max(int(hours), 1) + 1
        total: Counter = Counter()
        with self._lock:
            for hour, counter in self._buckets:
                if hour >= since:
                    total.update(counter)
        return [{"name": n, "count": c} for n, c in total.most_common()]


class RequestTagsMiddleware:
    """Pure ASGI middleware: extract tags + observe header names per HTTP
    request. A no-op when ``request_tags.enabled`` is false."""

    def __init__(self, app, get_state) -> None:
        self.app = app
        self._get_state = get_state

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        token = None
        try:
            gw = self._get_state()
            cfg = getattr(getattr(gw, "config", None), "request_tags", None)
            if cfg is not None and cfg.enabled:
                from starlette.datastructures import Headers

                headers = Headers(scope=scope)
                observer = getattr(gw, "header_observer", None)
                if observer is not None:
                    observer.observe(headers)
                token = current_tags.set(extract_tags(headers, cfg))
        except Exception:
            token = None
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                try:
                    current_tags.reset(token)
                except Exception:
                    pass


# ---------------------------------------------------------------- storage helpers
def parse_tag_filter(spec: Optional[str]) -> Optional[tuple[str, str]]:
    """``"name:value"`` -> ``(name, value)``; None when malformed."""
    if not spec or ":" not in spec:
        return None
    name, value = spec.split(":", 1)
    name = name.strip().lower()
    if not valid_tag_name(name) or not value:
        return None
    return name, value


def tag_like_pattern(name: str, value: str) -> str:
    """LIKE pattern (``ESCAPE '!'``) matching one tag in the canonical JSON
    stored by the gateway (compact separators, sorted keys)."""
    import json

    frag = json.dumps({name: value}, separators=(",", ":"))[1:-1]
    frag = frag.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{frag}%"


def aggregate_by_tag(rows: Any) -> dict:
    """``rows`` yields ``(tags_json, cost, tokens_in, tokens_out)``. Returns
    ``{tag: {value: {requests, cost, tokens_in, tokens_out}}}``."""
    import json

    out: dict = {}
    for tags_json, cost, tin, tout in rows:
        try:
            tags = json.loads(tags_json) if tags_json else {}
        except Exception:
            continue
        if not isinstance(tags, dict):
            continue
        for name, value in tags.items():
            slot = out.setdefault(str(name), {}).setdefault(
                str(value), {"requests": 0, "cost": 0.0, "tokens_in": 0, "tokens_out": 0}
            )
            slot["requests"] += 1
            slot["cost"] = round(slot["cost"] + float(cost or 0.0), 6)
            slot["tokens_in"] += int(tin or 0)
            slot["tokens_out"] += int(tout or 0)
    return out


def parse_tags_json(raw: Any) -> dict:
    import json

    try:
        val = json.loads(raw) if raw else {}
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}
