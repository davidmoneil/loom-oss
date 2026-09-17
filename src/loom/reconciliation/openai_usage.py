"""OpenAI usage API ingestion adapter.

Fetches organization-wide token usage from OpenAI's Usage API and normalizes
it into `ProviderUsageRecord` rows, bucketed by model and time window, for
comparison against Loom's own gateway metrics (AIProjects-srdn).

Requires an Admin API key (distinct from a regular OpenAI API key — see
https://platform.openai.com/docs/api-reference/usage). Read from the
`OPENAI_ADMIN_API_KEY` env var; never hardcoded.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from .models import ProviderUsageRecord

USAGE_COMPLETIONS_URL = "https://api.openai.com/v1/organization/usage/completions"


class OpenAIUsageError(RuntimeError):
    """Raised when the Admin API key is missing or the request fails."""


def _admin_api_key() -> str:
    key = os.environ.get("OPENAI_ADMIN_API_KEY")
    if not key:
        raise OpenAIUsageError(
            "OPENAI_ADMIN_API_KEY is not set — an Admin API key is required "
            "to call the OpenAI organization usage endpoint (distinct from a "
            "regular provider API key)."
        )
    return key


def normalize_openai_usage(payload: dict) -> list[ProviderUsageRecord]:
    """Convert a usage completions response into normalized records.

    Expected shape (one entry per time bucket in `data`, one row per
    model/dimension combination in `results`):

        {"data": [{"start_time": 0, "end_time": 0,
                    "results": [{"model": "...", "input_tokens": 0,
                                  "input_cached_tokens": 0,
                                  "output_tokens": 0,
                                  "num_model_requests": 0}]}]}

    Note: unlike Anthropic (whose cache components are split out and must be
    summed), OpenAI's `input_tokens` already includes `input_cached_tokens` —
    it must not be added again.
    """
    records: list[ProviderUsageRecord] = []
    for bucket in payload.get("data", []):
        window_start = datetime.fromtimestamp(bucket["start_time"], tz=timezone.utc)
        window_end = datetime.fromtimestamp(bucket["end_time"], tz=timezone.utc)
        for result in bucket.get("results", []):
            records.append(
                ProviderUsageRecord(
                    provider="openai",
                    model=result.get("model") or "unknown",
                    window_start=window_start,
                    window_end=window_end,
                    input_tokens=result.get("input_tokens", 0),
                    output_tokens=result.get("output_tokens", 0),
                    request_count=result.get("num_model_requests", 0),
                )
            )
    return records


async def fetch_openai_usage(
    start: datetime,
    end: datetime,
    *,
    bucket_width: str = "1d",
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[ProviderUsageRecord]:
    """Fetch and normalize usage for [start, end) from the OpenAI usage API.

    Paginates via `next_page` until `has_more` is false. Groups by model so
    each result row carries a model name.
    """
    key = api_key or _admin_api_key()
    headers = {"Authorization": f"Bearer {key}"}
    params = {
        "start_time": int(start.astimezone(timezone.utc).timestamp()),
        "end_time": int(end.astimezone(timezone.utc).timestamp()),
        "bucket_width": bucket_width,
        "group_by": ["model"],
    }

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=30.0)
    records: list[ProviderUsageRecord] = []
    try:
        page_params = dict(params)
        while True:
            resp = await http_client.get(
                USAGE_COMPLETIONS_URL, headers=headers, params=page_params
            )
            if resp.status_code != 200:
                raise OpenAIUsageError(
                    f"OpenAI usage request failed: {resp.status_code} {resp.text}"
                )
            payload = resp.json()
            records.extend(normalize_openai_usage(payload))
            if not payload.get("has_more"):
                break
            page_params = dict(params, page=payload["next_page"])
    finally:
        if owns_client:
            await http_client.aclose()

    return records
