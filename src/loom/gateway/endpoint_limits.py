"""Per-endpoint concurrency limits for upstream providers (local model boxes).

One budget per upstream *endpoint* (``host:port`` of ``api_base``), not per
provider: two providers pointing at the same llama-swap or Ollama box share a
budget, and a second box gets its own. A request waits up to
``queue_timeout_seconds`` for a slot, then fails with 429 so the caller can
retry later instead of piling more work onto a busy GPU.

An optional ``max_requests_per_minute`` adds a sliding 60 s start-rate budget
(shared the same way); over budget, a request waits for the window up to
``queue_timeout_seconds`` and then fails with 429 ``endpoint_rate_limited`` and
a ``Retry-After`` header.

Streaming responses hold their slot until the stream is fully consumed or
closed.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

from loom.gateway.providers.base import ProviderBackend, ProviderError


RATE_WINDOW_SECONDS = 60.0


def endpoint_key(api_base: str) -> str:
    """``http://host.docker.internal:9292/v1`` -> ``host.docker.internal:9292``."""
    u = urlparse(api_base if "://" in api_base else f"http://{api_base}")
    port = u.port or (443 if u.scheme == "https" else 80)
    return f"{(u.hostname or '').lower()}:{port}"


@dataclass
class EndpointLimit:
    """One endpoint's budget: a concurrency cap and/or a requests-per-minute cap.

    Either may be ``None`` (unlimited). Concurrency bounds simultaneous work;
    the rate budget bounds how often work *starts* in any sliding 60 s window,
    which concurrency alone cannot do for a fast sequential caller.
    """

    key: str
    max_concurrency: Optional[int]
    queue_timeout_seconds: float
    max_requests_per_minute: Optional[int] = None
    providers: list[str] = field(default_factory=list)
    in_flight: int = 0
    waiting: int = 0
    served: int = 0
    rejected: int = 0
    rate_limited: int = 0
    _starts: deque = field(default_factory=deque)
    _waiters: deque = field(default_factory=deque)

    # ------------------------------------------------------------ helpers
    def _prune(self, now: float) -> None:
        while self._starts and now - self._starts[0] >= RATE_WINDOW_SECONDS:
            self._starts.popleft()

    def _wake(self, everyone: bool = False) -> None:
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)
                if not everyone:
                    return

    def reconfigure(self, max_concurrency: Optional[int], queue_timeout_seconds: float,
                    max_requests_per_minute: Optional[int]) -> None:
        """Apply new limits to a live endpoint (in-flight requests are unaffected)."""
        self.max_concurrency = max_concurrency or None
        self.queue_timeout_seconds = queue_timeout_seconds
        self.max_requests_per_minute = max_requests_per_minute or None
        self._wake(everyone=True)

    # ------------------------------------------------------------ acquire
    async def _acquire_concurrency(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        while self.max_concurrency and self.in_flight >= self.max_concurrency:
            remaining = deadline - loop.time()
            fut = loop.create_future()
            self._waiters.append(fut)
            try:
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(fut, timeout=remaining)
            except asyncio.TimeoutError:
                self.rejected += 1
                raise ProviderError(
                    f"local endpoint {self.key} is at its concurrency limit "
                    f"({self.max_concurrency}); retry later",
                    status_code=429,
                    payload={"error": {"type": "endpoint_busy", "endpoint": self.key,
                                       "message": "upstream concurrency limit reached; retry later"}},
                ) from None
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)

    async def _acquire_rate(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        while True:
            limit = self.max_requests_per_minute
            if not limit:
                return
            now = time.monotonic()
            self._prune(now)
            if len(self._starts) < limit:
                self._starts.append(now)
                return
            retry_after = self._starts[0] + RATE_WINDOW_SECONDS - now
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.rate_limited += 1
                secs = max(1, math.ceil(retry_after))
                err = ProviderError(
                    f"local endpoint {self.key} is over its rate budget "
                    f"({limit}/min); retry in {secs}s",
                    status_code=429,
                    payload={"error": {"type": "endpoint_rate_limited", "endpoint": self.key,
                                       "max_requests_per_minute": limit,
                                       "retry_after_seconds": secs,
                                       "message": "upstream request rate budget reached; retry later"}},
                )
                err.headers = {"Retry-After": str(secs)}
                raise err
            await asyncio.sleep(max(0.0, min(retry_after, remaining)))

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.queue_timeout_seconds
        self.waiting += 1
        try:
            await self._acquire_concurrency(deadline)
            # Hold the concurrency slot (reserved below) while waiting for the
            # rate window so a waiter cannot be overtaken past the cap.
            self.in_flight += 1
            try:
                await self._acquire_rate(deadline)
            except BaseException:
                self.in_flight -= 1
                self._wake()
                raise
        finally:
            self.waiting -= 1

    def release(self) -> None:
        self.in_flight -= 1
        self.served += 1
        self._wake()

    def snapshot(self) -> dict[str, Any]:
        self._prune(time.monotonic())
        return {"endpoint": self.key, "providers": self.providers,
                "max_concurrency": self.max_concurrency,
                "queue_timeout_seconds": self.queue_timeout_seconds,
                "max_requests_per_minute": self.max_requests_per_minute,
                "requests_last_minute": len(self._starts),
                "in_flight": self.in_flight, "waiting": self.waiting,
                "served": self.served, "rejected": self.rejected,
                "rate_limited": self.rate_limited}


def _strictest(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """Lowest non-null value; None only if both are None."""
    vals = [v for v in (a, b) if v]
    return min(vals) if vals else None


class EndpointLimiter:
    """Registry of per-endpoint limits."""

    def __init__(self) -> None:
        self._limits: dict[str, EndpointLimit] = {}

    def get(self, api_base: str) -> Optional[EndpointLimit]:
        return self._limits.get(endpoint_key(api_base))

    def register(self, provider: str, api_base: str, max_concurrency: Optional[int],
                 queue_timeout_seconds: float,
                 max_requests_per_minute: Optional[int] = None) -> Optional[EndpointLimit]:
        if not max_concurrency and not max_requests_per_minute:
            return None
        key = endpoint_key(api_base)
        lim = self._limits.get(key)
        if lim is None:
            lim = self._limits[key] = EndpointLimit(
                key, max_concurrency or None, queue_timeout_seconds,
                max_requests_per_minute or None)
        else:
            # Several providers on one box: the strictest setting wins.
            lim.max_concurrency = _strictest(lim.max_concurrency, max_concurrency)
            lim.max_requests_per_minute = _strictest(lim.max_requests_per_minute,
                                                     max_requests_per_minute)
            lim.queue_timeout_seconds = min(lim.queue_timeout_seconds, queue_timeout_seconds)
        lim.providers.append(provider)
        return lim

    def snapshot(self) -> list[dict[str, Any]]:
        return [lim.snapshot() for lim in self._limits.values()]


async def _release_after(stream: AsyncIterator[bytes], lim: EndpointLimit) -> AsyncIterator[bytes]:
    try:
        async for chunk in stream:
            yield chunk
    finally:
        lim.release()


class LimitedBackend(ProviderBackend):
    """Wraps a backend so every completion/generate call holds an endpoint slot."""

    def __init__(self, inner: ProviderBackend, limit: EndpointLimit):
        self._inner = inner
        self._limit = limit
        self.name = inner.name
        self.api_base = inner.api_base

    def __getattr__(self, item: str) -> Any:
        # list_models, set_config_models, etc. pass through unlimited.
        return getattr(self._inner, item)

    async def _limited(self, call, *args, **kwargs):
        await self._limit.acquire()
        try:
            result = await call(*args, **kwargs)
        except BaseException:
            self._limit.release()
            raise
        if hasattr(result, "__aiter__"):
            return _release_after(result, self._limit)
        self._limit.release()
        return result

    async def chat_completion(self, *args, **kwargs):
        return await self._limited(self._inner.chat_completion, *args, **kwargs)

    async def generate(self, *args, **kwargs):
        return await self._limited(self._inner.generate, *args, **kwargs)

    async def list_models(self) -> list[str]:
        return await self._inner.list_models()

    async def get_client(self):
        return await self._inner.get_client()

    async def close(self) -> None:
        await self._inner.close()
