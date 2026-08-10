"""Regression tests for the /compact indefinite-hang fix.

Both the compression phase and the upstream stream-forwarding loop
previously had no ceiling on how long they could wait, so a stall in
either (e.g. thread-pool contention under concurrent load, or a
stalled-but-still-pinging upstream) hung the request forever with no
error. These tests confirm each now fails fast with a 504 instead.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from loom.gateway.app import _iter_with_idle_timeout
from loom.gateway.providers.base import ProviderError


async def _stalls_forever():
    yield b"first chunk\n"
    await asyncio.sleep(3600)
    yield b"never reached\n"  # pragma: no cover


async def _completes_normally():
    yield b"a\n"
    yield b"b\n"


@pytest.mark.asyncio
async def test_iter_with_idle_timeout_raises_on_stall():
    with pytest.raises(ProviderError) as exc_info:
        async for _ in _iter_with_idle_timeout(_stalls_forever(), timeout=0.05, request_id="test-stall"):
            pass
    assert exc_info.value.status_code == 504
    assert "idle" in str(exc_info.value)


@pytest.mark.asyncio
async def test_iter_with_idle_timeout_passes_through_normal_stream():
    chunks = [
        chunk
        async for chunk in _iter_with_idle_timeout(_completes_normally(), timeout=5.0, request_id="test-ok")
    ]
    assert chunks == [b"a\n", b"b\n"]


class _FakeAnthropicBackend:
    """Never actually called: compression should time out first."""

    name = "anthropic"

    async def chat_completion(self, *args, **kwargs):
        raise AssertionError("should have timed out during compression")  # pragma: no cover

    async def list_models(self):
        return []  # pragma: no cover


def test_compression_phase_timeout_returns_504(monkeypatch):
    import time as time_module

    from loom.gateway import app as app_module

    def _slow_compress(*args, **kwargs):
        time_module.sleep(2)
        return args[1], 0, 0, {}, False  # pragma: no cover — should never finish

    monkeypatch.setattr(app_module, "_compress_messages_inline", _slow_compress)
    monkeypatch.setattr(app_module, "_COMPRESSION_TIMEOUT_SECONDS", 0.05)

    with TestClient(app_module.app) as client:
        gw = app_module.app.state.gateway
        if gw.compression is None:
            pytest.skip("compression processor not initialized in this environment")
        gw.backends["anthropic"] = _FakeAnthropicBackend()
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 10,
                "messages": [
                    {"role": "user", "content": f"message {i}"} for i in range(5)
                ],
            },
        )
    assert resp.status_code == 504
    assert "compression phase exceeded" in resp.text
