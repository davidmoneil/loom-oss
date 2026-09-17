"""Tests for the OpenAI usage API ingestion adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from loom.reconciliation.openai_usage import (
    OpenAIUsageError,
    fetch_openai_usage,
    normalize_openai_usage,
)

SAMPLE_PAYLOAD = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "start_time": 1755129600,  # 2025-08-14T00:00:00Z
            "end_time": 1755216000,  # 2025-08-15T00:00:00Z
            "results": [
                {
                    "object": "organization.usage.completions.result",
                    "model": "gpt-4o-2024-08-06",
                    "input_tokens": 1200,
                    "input_cached_tokens": 200,
                    "output_tokens": 300,
                    "num_model_requests": 12,
                }
            ],
        }
    ],
    "has_more": False,
    "next_page": None,
}


def test_normalize_openai_usage_does_not_double_count_cached_tokens():
    records = normalize_openai_usage(SAMPLE_PAYLOAD)

    assert len(records) == 1
    record = records[0]
    assert record.provider == "openai"
    assert record.model == "gpt-4o-2024-08-06"
    # input_tokens already includes input_cached_tokens — must not be summed again.
    assert record.input_tokens == 1200
    assert record.output_tokens == 300
    assert record.request_count == 12
    assert record.window_start == datetime(2025, 8, 14, tzinfo=timezone.utc)
    assert record.window_end == datetime(2025, 8, 15, tzinfo=timezone.utc)


def test_normalize_openai_usage_handles_multiple_buckets_and_models():
    payload = {
        "data": [
            SAMPLE_PAYLOAD["data"][0],
            {
                "start_time": 1755216000,
                "end_time": 1755302400,
                "results": [
                    {
                        "model": "gpt-4o-mini-2024-07-18",
                        "input_tokens": 5,
                        "input_cached_tokens": 0,
                        "output_tokens": 2,
                        "num_model_requests": 1,
                    }
                ],
            },
        ]
    }

    records = normalize_openai_usage(payload)

    assert len(records) == 2
    assert {r.model for r in records} == {
        "gpt-4o-2024-08-06",
        "gpt-4o-mini-2024-07-18",
    }


def test_normalize_openai_usage_empty_payload():
    assert normalize_openai_usage({"data": []}) == []


def test_normalize_openai_usage_missing_model_defaults_to_unknown():
    payload = {
        "data": [
            {
                "start_time": 1755129600,
                "end_time": 1755216000,
                "results": [
                    {
                        "input_tokens": 5,
                        "output_tokens": 1,
                        "num_model_requests": 1,
                    }
                ],
            }
        ]
    }

    records = normalize_openai_usage(payload)

    assert records[0].model == "unknown"


async def test_fetch_openai_usage_paginates_and_uses_bearer_auth():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer admin-key-123"
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
        records = await fetch_openai_usage(
            datetime(2025, 8, 14, tzinfo=timezone.utc),
            datetime(2025, 8, 16, tzinfo=timezone.utc),
            api_key="admin-key-123",
            client=client,
        )

    assert len(calls) == 2
    assert len(records) == 1


async def test_fetch_openai_usage_sends_unix_timestamps_and_group_by_model():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [], "has_more": False, "next_page": None})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await fetch_openai_usage(
            datetime(2025, 8, 14, tzinfo=timezone.utc),
            datetime(2025, 8, 16, tzinfo=timezone.utc),
            api_key="admin-key-123",
            client=client,
        )

    assert captured["params"]["start_time"] == str(int(datetime(2025, 8, 14, tzinfo=timezone.utc).timestamp()))
    assert captured["params"]["end_time"] == str(int(datetime(2025, 8, 16, tzinfo=timezone.utc).timestamp()))
    assert captured["params"]["group_by"] == "model"


async def test_fetch_openai_usage_raises_on_non_200():
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="unauthorized"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(OpenAIUsageError):
            await fetch_openai_usage(
                datetime(2025, 8, 14, tzinfo=timezone.utc),
                datetime(2025, 8, 16, tzinfo=timezone.utc),
                api_key="admin-key-123",
                client=client,
            )


def test_missing_admin_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_ADMIN_API_KEY", raising=False)
    from loom.reconciliation.openai_usage import _admin_api_key

    with pytest.raises(OpenAIUsageError):
        _admin_api_key()
