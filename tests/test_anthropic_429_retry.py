"""Regression tests for the bounded 429 retry in the Anthropic backend.

A 429 with a short retry-after is a transient per-minute/concurrency collision
between independent sessions sharing the gateway — previously this tore down
the client's stream with zero bytes sent, surfacing as "Streaming response
ended before any complete data was received" in Claude Code. These tests
confirm a short-retry-after 429 is now retried once transparently, while a
429 with no retry-after (or one beyond the transient-collision cap, e.g. the
weekly rolling quota) is still surfaced to the caller immediately.
"""

import httpx
import pytest

from loom.gateway.providers import anthropic as anthropic_module
from loom.gateway.providers.anthropic import AnthropicBackend
from loom.gateway.providers.base import ProviderError


def _make_backend(handler):
    backend = AnthropicBackend(api_base="https://api.anthropic.com")
    transport = httpx.MockTransport(handler)
    backend._client = httpx.AsyncClient(
        base_url=backend.api_base, transport=transport
    )
    return backend


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    calls = []

    async def _fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(anthropic_module.asyncio, "sleep", _fake_sleep)
    return calls


@pytest.mark.asyncio
async def test_complete_retries_once_on_short_retry_after(_no_real_sleep):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "rate_limited"})
        return httpx.Response(200, json={"id": "msg_1", "content": []})

    backend = _make_backend(handler)
    result = await backend._complete({"model": "claude-haiku-4-5-20251001"}, api_key="sk-test")

    assert result == {"id": "msg_1", "content": []}
    assert attempts["n"] == 2
    assert _no_real_sleep == [2.0]


@pytest.mark.asyncio
async def test_complete_raises_immediately_when_no_retry_after(_no_real_sleep):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, json={"error": "rate_limited"})

    backend = _make_backend(handler)
    with pytest.raises(ProviderError) as exc_info:
        await backend._complete({"model": "claude-haiku-4-5-20251001"}, api_key="sk-test")

    assert exc_info.value.status_code == 429
    assert attempts["n"] == 1
    assert _no_real_sleep == []


@pytest.mark.asyncio
async def test_complete_raises_immediately_when_retry_after_exceeds_cap(_no_real_sleep):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        # e.g. the weekly rolling quota — hours, not seconds.
        return httpx.Response(429, headers={"retry-after": "3600"}, json={"error": "rate_limited"})

    backend = _make_backend(handler)
    with pytest.raises(ProviderError):
        await backend._complete({"model": "claude-haiku-4-5-20251001"}, api_key="sk-test")

    assert attempts["n"] == 1
    assert _no_real_sleep == []


@pytest.mark.asyncio
async def test_stream_retries_once_on_short_retry_after_then_yields_normally(_no_real_sleep):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": "rate_limited"})

        async def _abody():
            yield b"event: message\ndata: {}\n\n"

        # An async byte generator (rather than a raw bytes body) keeps httpx
        # from marking the response content as already consumed, so
        # aiter_raw() can actually stream it on an AsyncClient — matching
        # how a real upstream response behaves.
        return httpx.Response(200, content=_abody())

    backend = _make_backend(handler)
    chunks = [
        chunk
        async for chunk in backend._stream(
            {"model": "claude-haiku-4-5-20251001"}, api_key="sk-test"
        )
    ]

    assert b"".join(chunks) == b"event: message\ndata: {}\n\n"
    assert attempts["n"] == 2
    assert _no_real_sleep == [1.0]


@pytest.mark.asyncio
async def test_stream_raises_immediately_when_no_retry_after(_no_real_sleep):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, json={"error": "rate_limited"})

    backend = _make_backend(handler)
    with pytest.raises(ProviderError) as exc_info:
        async for _ in backend._stream(
            {"model": "claude-haiku-4-5-20251001"}, api_key="sk-test"
        ):
            pass

    assert exc_info.value.status_code == 429
    assert attempts["n"] == 1
    assert _no_real_sleep == []
