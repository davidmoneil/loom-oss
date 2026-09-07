"""Normalized schema shared by all provider usage ingestion adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProviderUsageRecord:
    """One provider-reported usage bucket, normalized for reconciliation
    against Loom's own gateway metrics.

    `provider` + `model` + `window_start` + `window_end` identify the bucket;
    downstream comparison groups Loom's `/api/metrics` rows by the same key.
    """

    provider: str
    model: str
    window_start: datetime
    window_end: datetime
    input_tokens: int
    output_tokens: int
    request_count: int
