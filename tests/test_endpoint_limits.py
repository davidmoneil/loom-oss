"""Per-endpoint concurrency limits for local model boxes."""

import asyncio

import pytest

from loom.config import ProviderConfig
from loom.gateway.endpoint_limits import EndpointLimiter, LimitedBackend, endpoint_key
from loom.gateway.providers.base import ProviderBackend, ProviderError


class _SlowBackend(ProviderBackend):
    name = "fake"

    def __init__(self, api_base="http://box:9292/v1", delay=0.05):
        super().__init__(api_base)
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def chat_completion(self, model, messages, api_key, stream=False, **kw):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if stream:
                async def gen():
                    yield b"a"
                    yield b"b"
                return gen()
            return {"ok": True}
        finally:
            self.active -= 1

    async def list_models(self):
        return ["m"]


def test_endpoint_key_normalizes_host_and_port():
    assert endpoint_key("http://host.docker.internal:9292/v1") == "host.docker.internal:9292"
    assert endpoint_key("http://Host.Docker.Internal:11434") == "host.docker.internal:11434"
    assert endpoint_key("https://api.openai.com/v1") == "api.openai.com:443"


def test_unlimited_provider_is_not_registered():
    lim = EndpointLimiter()
    assert lim.register("openai", "https://api.openai.com/v1", None, 30) is None
    assert lim.snapshot() == []


def test_providers_on_same_box_share_one_budget_strictest_wins():
    lim = EndpointLimiter()
    a = lim.register("llamaswap", "http://box:9292/v1", 3, 30)
    b = lim.register("llamaswap-alt", "http://box:9292", 2, 10)
    assert a is b and a.max_concurrency == 2 and a.queue_timeout_seconds == 10
    assert a.providers == ["llamaswap", "llamaswap-alt"]


def test_separate_boxes_get_separate_budgets():
    lim = EndpointLimiter()
    a = lim.register("box1", "http://box1:9292/v1", 2, 30)
    b = lim.register("box2", "http://box2:9292/v1", 2, 30)
    assert a is not b and len(lim.snapshot()) == 2


def test_concurrency_is_capped():
    async def run():
        inner = _SlowBackend()
        limit = EndpointLimiter().register("fake", inner.api_base, 2, 5)
        be = LimitedBackend(inner, limit)
        await asyncio.gather(*[be.chat_completion("m", [], "") for _ in range(6)])
        return inner.peak, limit.snapshot()
    peak, snap = asyncio.run(run())
    assert peak == 2
    assert snap["served"] == 6 and snap["in_flight"] == 0 and snap["rejected"] == 0


def test_queue_timeout_returns_429():
    async def run():
        inner = _SlowBackend(delay=0.5)
        limit = EndpointLimiter().register("fake", inner.api_base, 1, 0.05)
        be = LimitedBackend(inner, limit)
        results = await asyncio.gather(be.chat_completion("m", [], ""), be.chat_completion("m", [], ""),
                                       return_exceptions=True)
        return results, limit
    results, limit = asyncio.run(run())
    errs = [r for r in results if isinstance(r, ProviderError)]
    assert len(errs) == 1 and errs[0].status_code == 429
    assert limit.rejected == 1 and limit.in_flight == 0


def test_stream_holds_slot_until_consumed():
    async def run():
        inner = _SlowBackend(delay=0)
        limit = EndpointLimiter().register("fake", inner.api_base, 1, 5)
        be = LimitedBackend(inner, limit)
        stream = await be.chat_completion("m", [], "", stream=True)
        held = limit.in_flight
        chunks = [c async for c in stream]
        return held, chunks, limit.in_flight
    held, chunks, after = asyncio.run(run())
    assert held == 1 and chunks == [b"a", b"b"] and after == 0


def test_error_releases_slot():
    class Boom(_SlowBackend):
        async def chat_completion(self, *a, **k):
            raise ProviderError("down", 502)

    async def run():
        inner = Boom()
        limit = EndpointLimiter().register("fake", inner.api_base, 1, 5)
        be = LimitedBackend(inner, limit)
        with pytest.raises(ProviderError):
            await be.chat_completion("m", [], "")
        return limit.in_flight
    assert asyncio.run(run()) == 0


def test_passthrough_methods_unlimited():
    inner = _SlowBackend()
    limit = EndpointLimiter().register("fake", inner.api_base, 1, 5)
    be = LimitedBackend(inner, limit)
    assert asyncio.run(be.list_models()) == ["m"]
    assert be.name == "fake"


def test_config_defaults_unlimited():
    p = ProviderConfig(name="x", api_base="http://x")
    assert p.max_concurrency is None and p.queue_timeout_seconds == 30.0
