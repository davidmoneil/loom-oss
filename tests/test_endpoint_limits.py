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


# ------------------------------------------------------------------ rate budget
def test_config_rate_default_is_none():
    assert ProviderConfig(name="x", api_base="http://x").max_requests_per_minute is None


def test_rate_only_provider_registers_and_snapshot_shows_it():
    lim = EndpointLimiter()
    a = lim.register("p", "http://box:9292/v1", None, 5, 6)
    assert a is not None and a.max_concurrency is None
    snap = lim.snapshot()[0]
    assert snap["max_requests_per_minute"] == 6 and snap["rate_limited"] == 0


def test_rate_budget_lowest_non_null_wins_when_sharing():
    lim = EndpointLimiter()
    a = lim.register("a", "http://box:9292/v1", 3, 30, None)
    b = lim.register("b", "http://box:9292/v1", 3, 30, 10)
    c = lim.register("c", "http://box:9292/v1", 3, 30, 6)
    assert a is b is c and a.max_requests_per_minute == 6


def test_seventh_request_in_window_gets_429_with_retry_after():
    async def go():
        inner = _SlowBackend(delay=0)
        limit = EndpointLimiter().register("fake", inner.api_base, None, 0.05, 6)
        be = LimitedBackend(inner, limit)
        for _ in range(6):
            assert await be.chat_completion("m", [], "k") == {"ok": True}
        with pytest.raises(ProviderError) as ei:
            await be.chat_completion("m", [], "k")
        return ei.value, limit

    err, limit = asyncio.run(go())
    assert err.status_code == 429
    assert err.payload["error"]["type"] == "endpoint_rate_limited"
    assert 1 <= int(err.headers["Retry-After"]) <= 60
    assert limit.rate_limited == 1 and limit.rejected == 0 and limit.in_flight == 0
    assert limit.snapshot()["requests_last_minute"] == 6


def test_rate_waits_for_window_then_succeeds(monkeypatch):
    import loom.gateway.endpoint_limits as el
    monkeypatch.setattr(el, "RATE_WINDOW_SECONDS", 0.2)

    async def go():
        inner = _SlowBackend(delay=0)
        limit = EndpointLimiter().register("fake", inner.api_base, None, 5, 2)
        be = LimitedBackend(inner, limit)
        t0 = asyncio.get_running_loop().time()
        for _ in range(3):
            await be.chat_completion("m", [], "k")
        return asyncio.get_running_loop().time() - t0, limit

    elapsed, limit = asyncio.run(go())
    assert elapsed >= 0.19 and limit.rate_limited == 0 and limit.served == 3


def test_no_limits_configured_behaves_as_before():
    lim = EndpointLimiter()
    assert lim.register("x", "http://box:1/v1", None, 30, None) is None
    assert lim.snapshot() == []


def test_failed_rate_acquire_releases_concurrency_slot():
    async def go():
        inner = _SlowBackend(delay=0)
        limit = EndpointLimiter().register("fake", inner.api_base, 1, 0.05, 1)
        be = LimitedBackend(inner, limit)
        await be.chat_completion("m", [], "k")
        with pytest.raises(ProviderError):
            await be.chat_completion("m", [], "k")
        return limit

    limit = asyncio.run(go())
    assert limit.in_flight == 0 and limit.waiting == 0


def test_reconfigure_live_changes_limits():
    async def go():
        inner = _SlowBackend(delay=0)
        limit = EndpointLimiter().register("fake", inner.api_base, 1, 0.05, 1)
        be = LimitedBackend(inner, limit)
        await be.chat_completion("m", [], "k")
        limit.reconfigure(1, 0.05, 5)
        await be.chat_completion("m", [], "k")
        limit.reconfigure(None, 0.05, None)
        for _ in range(10):
            await be.chat_completion("m", [], "k")
        return limit

    limit = asyncio.run(go())
    assert limit.served == 12 and limit.max_requests_per_minute is None


# ------------------------------------------------------------------ API
def _api_client():
    from fastapi.testclient import TestClient
    from loom.config import LoomConfig
    from loom.gateway.app import app
    gw = app.state.gateway
    return TestClient(app), gw


def test_provider_patch_updates_live_limiter_and_http_429_has_retry_after():
    from loom.config import LoomConfig
    client, gw = _api_client()
    prev_cfg, prev_lim = gw.config, gw.endpoint_limiter
    try:
        gw.config = LoomConfig(providers=[
            ProviderConfig(name="box", api_base="http://box:9292/v1", max_concurrency=2),
            ProviderConfig(name="cloud", api_base="https://api.example.com/v1"),
        ])
        gw.endpoint_limiter = EndpointLimiter()
        gw.endpoint_limiter.register("box", "http://box:9292/v1", 2, 30.0)

        r = client.patch("/api/config/providers/box",
                         json={"max_requests_per_minute": 6, "queue_timeout_seconds": 5,
                               "max_concurrency": 3})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["restart_required"] is False
        assert body["endpoint"]["max_requests_per_minute"] == 6
        assert body["endpoint"]["max_concurrency"] == 3
        assert body["endpoint"]["queue_timeout_seconds"] == 5.0
        assert gw.config.providers[0].max_requests_per_minute == 6
        assert gw.endpoint_limiter.get("http://box:9292/v1").max_requests_per_minute == 6

        # null clears the rate budget live
        r = client.patch("/api/config/providers/box", json={"max_requests_per_minute": None})
        assert r.json()["endpoint"]["max_requests_per_minute"] is None
        assert client.get("/api/config/providers/box").json()["max_concurrency"] == 3

        # validation + unknown provider
        assert client.patch("/api/config/providers/box",
                            json={"max_requests_per_minute": -1}).status_code == 400
        assert client.patch("/api/config/providers/box", json={"nope": 1}).status_code == 400
        assert client.patch("/api/config/providers/zzz", json={"max_concurrency": 1}).status_code == 404

        # provider with no limiter at startup: saved, restart flagged
        r = client.patch("/api/config/providers/cloud", json={"max_requests_per_minute": 10})
        assert r.json()["restart_required"] is True
    finally:
        gw.config, gw.endpoint_limiter = prev_cfg, prev_lim


def test_error_response_carries_retry_after_header():
    from loom.gateway.app import _error_response
    err = ProviderError("x", status_code=429)
    err.headers = {"Retry-After": "7"}
    resp = _error_response(err, "rid")
    assert resp.status_code == 429 and resp.headers["retry-after"] == "7"


def test_provider_patch_rejects_non_finite_queue_timeout():
    from loom.config import LoomConfig
    client, gw = _api_client()
    prev_cfg, prev_lim = gw.config, gw.endpoint_limiter
    try:
        gw.config = LoomConfig(providers=[
            ProviderConfig(name="box", api_base="http://box:9292/v1", max_concurrency=2)])
        gw.endpoint_limiter = EndpointLimiter()
        gw.endpoint_limiter.register("box", "http://box:9292/v1", 2, 30.0)
        for bad in ("NaN", "Infinity"):
            r = client.patch("/api/config/providers/box", content=f'{{"queue_timeout_seconds": {bad}}}',
                             headers={"Content-Type": "application/json"})
            assert r.status_code == 400, (bad, r.text)
        assert client.get("/api/config/providers/box").json()["queue_timeout_seconds"] == 30.0
    finally:
        gw.config, gw.endpoint_limiter = prev_cfg, prev_lim
