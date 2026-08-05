"""Pydantic response models for the gateway's observability/dashboard API.

These document the JSON contract for /docs (Swagger). Most of the underlying
data originates from storage/scanner/governor internals whose exact per-record
shape can evolve independently of this file, so nested/dynamic sections are
typed loosely (dict/list[dict]) rather than fully nested models - the goal is
an accurate top-level contract, not runtime validation of every field.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ObservabilityResponse(BaseModel):
    """Base for observability response models - tolerates extra fields so
    schema drift in underlying storage/scanner/governor data never causes a
    response_model validation error."""

    model_config = ConfigDict(extra="allow")


class HealthResponse(ObservabilityResponse):
    status: str
    version: str | None = None
    # False means no gateway keys exist and the API is running open.
    auth_enabled: bool | None = None
    uptime_seconds: float | None = None
    checks: dict[str, Any] = {}


class CostTotals(ObservabilityResponse):
    requests: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    tokens_saved: int
    savings_usd: float


class CostSummaryResponse(ObservabilityResponse):
    window_days: int
    totals: CostTotals
    by_model: list[dict[str, Any]] = []
    by_source: list[dict[str, Any]] = []
    by_tier: list[dict[str, Any]] = []
    by_day: list[dict[str, Any]] = []
    by_hour: list[dict[str, Any]] = []


class CompressionSummaryResponse(ObservabilityResponse):
    available: bool = True
    window_days: int | None = None
    totals: dict[str, Any] = {}
    ratio_histogram: list[dict[str, Any]] = []
    by_tier: list[dict[str, Any]] = []
    by_model: list[dict[str, Any]] = []
    by_source: list[dict[str, Any]] = []
    by_day: list[dict[str, Any]] = []


class SessionListResponse(ObservabilityResponse):
    supported: bool
    sessions: int
    total_turns: int
    hours: int | None = None
    entries: list[dict[str, Any]] = []


class RoutingStatsResponse(ObservabilityResponse):
    """Routing-decision counters and tier breakdown from RoutingEngine.get_stats()."""


class ModelListResponse(ObservabilityResponse):
    models: list[dict[str, Any]] = []


class MetricsResponse(ObservabilityResponse):
    """Point-in-time gateway metrics snapshot."""


class MetricsTimeseriesResponse(ObservabilityResponse):
    window_hours: int
    interval_minutes: int
    buckets: list[dict[str, Any]] = []


class AuditPageResponse(ObservabilityResponse):
    total: int
    offset: int
    limit: int
    entries: list[dict[str, Any]] = []


class AuditContentResponse(ObservabilityResponse):
    """Full logged prompt/response record for one audit entry - fields beyond
    the identifying keys are whatever was captured at request time."""


class ConfigResponse(ObservabilityResponse):
    """Secret-scrubbed view of the active GatewayConfig - returned as-is by
    both the read endpoint and the server/source-policy mutation endpoints."""


class ScannerRulesResponse(ObservabilityResponse):
    enabled: bool
    rules: list[dict[str, Any]] = []
    skip_config: dict[str, Any] = {}


class ScannerRuleUpdateResponse(ObservabilityResponse):
    status: str
    rule: str
    updates: dict[str, Any] = {}


class ScannerStatsResponse(ObservabilityResponse):
    """Scanner hit/detection counters, or `{"enabled": false}` when disabled."""

    enabled: bool | None = None


class GovernorStatusResponse(ObservabilityResponse):
    """Throttle governor tier/state snapshot."""


class GovernorSettingsResponse(ObservabilityResponse):
    """Governor tier thresholds and per-job overrides."""


class GovernorOverrideDeleteResponse(ObservabilityResponse):
    """Governor settings snapshot after removing a per-job override."""


class RateLimitResponse(ObservabilityResponse):
    provider: str
    current: dict[str, Any] = {}
    trend: dict[str, Any] = {}


class ErrorResponse(ObservabilityResponse):
    error: str
