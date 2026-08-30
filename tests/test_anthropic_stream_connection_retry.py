"""Regression tests for the bounded connection-error retry in _stream().

A connection-level failure (a stale pooled keep-alive connection the peer
already closed, a reset mid-handshake, etc.) that happens before any bytes
have reached the client was previously unhandled in AnthropicBackend._stream()
— unlike _complete(), which already wraps its request in
`except httpx.HTTPError`. An uncaught exception there tore down the SSE
stream with zero bytes sent, surfacing to Claude Code as "Streaming response
ended before any complete data was received" — the same client-visible
symptom PR #84 fixed for short-retry-after 429s, just from a different
trigger. These tests confirm _stream() now retries once, transparently, on a
connection-level failure with no data sent yet; still raises (as a
ProviderError, not a raw httpx exception) if the retry also fails; and does
not attempt to retry once any chunk has already reached the caller.
"""

import httpx
import pytest

from loom.gateway.providers.anthropic import AnthropicBackend
from loom.gateway.providers.base import ProviderError


def _make_backend(handler):
    backend = AnthropicBackend(api_base="https://api.anthropic.com")
    transport = httpx.MockTransport(handler)
    backend._client = httpx.AsyncClient(
        base_url=backend.api_base, transport=transport
    )
    return backend


@pytest.mark.asyncio
async def test_stream_retries_once_on_connection_error_before_any_data():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

        async def _abody():
            yield b"event: message\ndata: {}\n\n"

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


@pytest.mark.asyncio
async def test_stream_raises_provider_error_when_connection_retry_also_fails():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("connection refused")

    backend = _make_backend(handler)
    with pytest.raises(ProviderError):
        async for _ in backend._stream(
            {"model": "claude-haiku-4-5-20251001"}, api_key="sk-test"
        ):
            pass

    # Initial attempt + one bounded retry — never a raw httpx exception,
    # and never retried indefinitely.
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_stream_does_not_retry_once_data_already_sent():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1

        async def _abody():
            yield b"event: message\ndata: {\"partial\": true}\n\n"
            raise httpx.ReadError("connection reset while reading")

        return httpx.Response(200, content=_abody())

    backend = _make_backend(handler)
    received = []
    with pytest.raises(ProviderError):
        async for chunk in backend._stream(
            {"model": "claude-haiku-4-5-20251001"}, api_key="sk-test"
        ):
            received.append(chunk)

    # The first chunk made it out before the failure — retrying now would
    # duplicate/corrupt the stream, so it must not be attempted again.
    assert b"".join(received) == b'event: message\ndata: {"partial": true}\n\n'
    assert attempts["n"] == 1
