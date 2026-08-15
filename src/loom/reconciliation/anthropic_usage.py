"""Anthropic Admin API usage ingestion adapter.

Fetches organization-wide token usage from the Anthropic Admin API's usage
report endpoint and normalizes it into `ProviderUsageRecord` rows, bucketed
by model and time window, for comparison against Loom's own gateway metrics
(AIProjects-srdn).

Requires an Admin API key (distinct from a regular Anthropic API key — see
https://docs.anthropic.com/en/api/admin-api/usage-cost/get-usage-report-messages).
Read from the `ANTHROPIC_ADMIN_API_KEY` env var; never hardcoded.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from .models import ProviderUsageRecord

ANTHROPIC_VERSION = "2023-06-01"
USAGE_REPORT_URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"


class AnthropicUsageError(RuntimeError):
    """Raised when the Admin API key is missing or the request fails."""


def _admin_api_key() -> str:
    key = os.environ.get("ANTHROPIC_ADMIN_API_KEY")
    if not key:
        raise AnthropicUsageError(
            "ANTHROPIC_ADMIN_API_KEY is not set — an Admin API key is required "
            "to call the Anthropic usage report endpoint (distinct from a "
            "regular provider API key)."
        )
    return key


def normalize_anthropic_usage(payload: dict) -> list[ProviderUsageRecord]:
    """Convert a usage report response into normalized records.

    Expected shape (one entry per time bucket in `data`, one row per
    model/dimension combination in `results`):

        {"data": [{"starting_at": "...", "ending_at": "...",
                    "results": [{"model": "...", "uncached_input_tokens": 0,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                      "ephemeral_1h_input_tokens": 0},
                                  "output_tokens": 0, "num_requests": 0}]}]}
    """
    records: list[ProviderUsageRecord] = []
    for bucket in payload.get("data", []):
        window_start = datetime.fromisoformat(bucket["starting_at"].replace("Z", "+00:00"))
        window_end = datetime.fromisoformat(bucket["ending_at"].replace("Z", "+00:00"))
        for result in bucket.get("results", []):
            cache_creation = result.get("cache_creation") or {}
            input_tokens = (
                result.get("uncached_input_tokens", 0)
                + result.get("cache_read_input_tokens", 0)
                + sum(cache_creation.values())
            )
            records.append(
                ProviderUsageRecord(
                    provider="anthropic",
                    model=result.get("model", "unknown"),
                    window_start=window_start,
                    window_end=window_end,
                    input_tokens=input_tokens,
                    output_tokens=result.get("output_tokens", 0),
                    request_count=result.get("num_requests", 0),
                )
            )
    return records


async def fetch_anthropic_usage(
    start: datetime,
    end: datetime,
    *,
    bucket_width: str = "1d",
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[ProviderUsageRecord]:
    """Fetch and normalize usage for [start, end) from the Anthropic Admin API.

    Paginates via `next_page` until `has_more` is false.
    """
    key = api_key or _admin_api_key()
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    params = {
        "starting_at": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ending_at": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bucket_width": bucket_width,
    }

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=30.0)
    records: list[ProviderUsageRecord] = []
    try:
        page_params = dict(params)
        while True:
            resp = await http_client.get(USAGE_REPORT_URL, headers=headers, params=page_params)
            if resp.status_code != 200:
                raise AnthropicUsageError(
                    f"Anthropic usage report request failed: {resp.status_code} {resp.text}"
                )
            payload = resp.json()
            records.extend(normalize_anthropic_usage(payload))
            if not payload.get("has_more"):
                break
            page_params = dict(params, page=payload["next_page"])
    finally:
        if owns_client:
            await http_client.aclose()

    return records
