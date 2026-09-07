"""Tests for the Anthropic usage report ingestion adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from loom.reconciliation.anthropic_usage import (
    AnthropicUsageError,
    fetch_anthropic_usage,
    normalize_anthropic_usage,
)

SAMPLE_PAYLOAD = {
    "data": [
        {
            "starting_at": "2026-08-14T00:00:00Z",
            "ending_at": "2026-08-15T00:00:00Z",
            "results": [
                {
                    "model": "claude-sonnet-4-20250514",
                    "uncached_input_tokens": 1000,
                    "cache_read_input_tokens": 200,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 50,
                        "ephemeral_1h_input_tokens": 0,
                    },
                    "output_tokens": 300,
                    "num_requests": 12,
                }
            ],
        }
    ],
    "has_more": False,
    "next_page": None,
}


def test_normalize_anthropic_usage_sums_input_token_components():
    records = normalize_anthropic_usage(SAMPLE_PAYLOAD)

    assert len(records) == 1
    record = records[0]
    assert record.provider == "anthropic"
    assert record.model == "claude-sonnet-4-20250514"
    assert record.input_tokens == 1000 + 200 + 50 + 0
    assert record.output_tokens == 300
    assert record.request_count == 12
    assert record.window_start == datetime(2026, 8, 14, tzinfo=timezone.utc)
    assert record.window_end == datetime(2026, 8, 15, tzinfo=timezone.utc)


def test_normalize_anthropic_usage_handles_multiple_buckets_and_models():
    payload = {
        "data": [
            SAMPLE_PAYLOAD["data"][0],
            {
                "starting_at": "2026-08-15T00:00:00Z",
                "ending_at": "2026-08-16T00:00:00Z",
                "results": [
                    {
                        "model": "claude-haiku-4-5-20251001",
                        "uncached_input_tokens": 5,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 2,
                        "num_requests": 1,
                    }
                ],
            },
        ]
    }

    records = normalize_anthropic_usage(payload)

    assert len(records) == 2
    assert {r.model for r in records} == {
        "claude-sonnet-4-20250514",
        "claude-haiku-4-5-20251001",
    }


def test_normalize_anthropic_usage_empty_payload():
    assert normalize_anthropic_usage({"data": []}) == []


async def test_fetch_anthropic_usage_paginates_and_uses_admin_key():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["x-api-key"] == "admin-key-123"
        if "page" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [SAMPLE_PAYLOAD["data"][0]],
                    "has_more": True,
                    "next_page": "page_2",
                },
            )
        return httpx.Response(200, json={"data": [], "has_more": False, "next_page": None})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        records = await fetch_anthropic_usage(
            datetime(2026, 8, 14, tzinfo=timezone.utc),
            datetime(2026, 8, 16, tzinfo=timezone.utc),
            api_key="admin-key-123",
            client=client,
        )

    assert len(calls) == 2
    assert len(records) == 1


async def test_fetch_anthropic_usage_raises_on_non_200():
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="unauthorized"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AnthropicUsageError):
            await fetch_anthropic_usage(
                datetime(2026, 8, 14, tzinfo=timezone.utc),
                datetime(2026, 8, 16, tzinfo=timezone.utc),
                api_key="admin-key-123",
                client=client,
            )


def test_missing_admin_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ADMIN_API_KEY", raising=False)
    from loom.reconciliation.anthropic_usage import _admin_api_key

    with pytest.raises(AnthropicUsageError):
        _admin_api_key()
