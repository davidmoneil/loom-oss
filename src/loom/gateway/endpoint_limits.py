"""Per-endpoint concurrency limits for upstream providers (local model boxes).

One budget per upstream *endpoint* (``host:port`` of ``api_base``), not per
provider: two providers pointing at the same llama-swap or Ollama box share a
budget, and a second box gets its own. A request waits up to
``queue_timeout_seconds`` for a slot, then fails with 429 so the caller can
retry later instead of piling more work onto a busy GPU.

Streaming responses hold their slot until the stream is fully consumed or
closed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

from loom.gateway.providers.base import ProviderBackend, ProviderError


def endpoint_key(api_base: str) -> str:
    """``http://host.docker.internal:9292/v1`` -> ``host.docker.internal:9292``."""
    u = urlparse(api_base if "://" in api_base else f"http://{api_base}")
    port = u.port or (443 if u.scheme == "https" else 80)
    return f"{(u.hostname or '').lower()}:{port}"


@dataclass
class EndpointLimit:
    key: str
    max_concurrency: int
    queue_timeout_seconds: float
    providers: list[str] = field(default_factory=list)
    in_flight: int = 0
    waiting: int = 0
    served: int = 0
    rejected: int = 0
    _sem: Optional[asyncio.Semaphore] = None

    @property
    def sem(self) -> asyncio.Semaphore:
        # Created lazily so it binds to the running event loop.
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrency)
        return self._sem

    async def acquire(self) -> None:
        self.waiting += 1
        try:
            await asyncio.wait_for(self.sem.acquire(), timeout=self.queue_timeout_seconds)
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
            self.waiting -= 1
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight -= 1
        self.served += 1
        self.sem.release()

    def snapshot(self) -> dict[str, Any]:
        return {"endpoint": self.key, "providers": self.providers,
                "max_concurrency": self.max_concurrency,
                "queue_timeout_seconds": self.queue_timeout_seconds,
                "in_flight": self.in_flight, "waiting": self.waiting,
                "served": self.served, "rejected": self.rejected}


class EndpointLimiter:
    """Registry of per-endpoint limits."""

    def __init__(self) -> None:
        self._limits: dict[str, EndpointLimit] = {}

    def register(self, provider: str, api_base: str, max_concurrency: Optional[int],
                 queue_timeout_seconds: float) -> Optional[EndpointLimit]:
        if not max_concurrency:
            return None
        key = endpoint_key(api_base)
        lim = self._limits.get(key)
        if lim is None:
            lim = self._limits[key] = EndpointLimit(key, max_concurrency, queue_timeout_seconds)
        else:
            # Several providers on one box: the strictest setting wins.
            lim.max_concurrency = min(lim.max_concurrency, max_concurrency)
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
