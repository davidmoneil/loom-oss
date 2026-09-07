"""Anthropic provider backend.

Targets the Anthropic Messages API (``/v1/messages``). Requests use the
Anthropic-native message format (``role`` + ``content`` blocks) and authenticate
via the ``x-api-key`` header plus a pinned ``anthropic-version``.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx

from .base import ProviderBackend, ProviderError

ANTHROPIC_VERSION = "2023-06-01"

# A 429 with a short retry-after is a transient per-minute/concurrency collision
# (e.g. several sessions hitting the account's rate limit in the same moment) —
# worth one bounded retry so the client never sees a torn-down stream for it.
# A 429 with no retry-after, or a long one (e.g. the weekly rolling quota), means
# genuine exhaustion — retrying would just hang the connection, so it's surfaced
# to the client immediately instead.
_MAX_429_RETRY_AFTER_SECONDS = 10.0
_MAX_429_RETRIES = 1

# A connection-level failure (peer closed a stale pooled keep-alive connection,
# a reset mid-handshake, etc.) that happens before any bytes have reached the
# client is safe to retry once with a fresh connection — same rationale as the
# 429 case above: the alternative is tearing down the SSE stream with zero
# bytes sent, which surfaces to Claude Code as "Streaming response ended
# before any complete data was received". Once any chunk has been yielded,
# retrying would duplicate/corrupt the stream, so it's not attempted.
_MAX_STREAM_CONN_RETRIES = 1


def _short_retry_after(resp: httpx.Response, cap: float = _MAX_429_RETRY_AFTER_SECONDS) -> float | None:
    """Return retry-after in seconds if present and within the transient-collision cap, else None.

    REPO_META capability=gateway.provider.retry-policy
    REPO_META purpose="Decides whether a 429 response is a short transient collision worth one bounded retry, versus genuine quota exhaustion that must surface immediately."
    REPO_META role=policy
    """
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds <= 0 or seconds > cap:
        return None
    return seconds

# Anthropic has no public "list models" endpoint, so this is a maintained list.
_KNOWN_MODELS = [
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
]


_RATELIMIT_HEADER_MAP = {
    "anthropic-ratelimit-requests-limit": "ratelimit_requests_limit",
    "anthropic-ratelimit-requests-remaining": "ratelimit_requests_remaining",
    "anthropic-ratelimit-requests-reset": "ratelimit_requests_reset",
    "anthropic-ratelimit-tokens-limit": "ratelimit_tokens_limit",
    "anthropic-ratelimit-tokens-remaining": "ratelimit_tokens_remaining",
    "anthropic-ratelimit-tokens-reset": "ratelimit_tokens_reset",
    "anthropic-ratelimit-input-tokens-limit": "ratelimit_input_tokens_limit",
    "anthropic-ratelimit-input-tokens-remaining": "ratelimit_input_tokens_remaining",
    "anthropic-ratelimit-input-tokens-reset": "ratelimit_input_tokens_reset",
    "anthropic-ratelimit-output-tokens-limit": "ratelimit_output_tokens_limit",
    "anthropic-ratelimit-output-tokens-remaining": "ratelimit_output_tokens_remaining",
    "anthropic-ratelimit-output-tokens-reset": "ratelimit_output_tokens_reset",
    "retry-after": "retry_after",
    "anthropic-ratelimit-unified-status": "ratelimit_unified_status",
    "anthropic-ratelimit-unified-reset": "ratelimit_unified_reset",
    "anthropic-ratelimit-unified-5h-utilization": "ratelimit_unified_5h_utilization",
    "anthropic-ratelimit-unified-5h-status": "ratelimit_unified_5h_status",
    "anthropic-ratelimit-unified-7d-utilization": "ratelimit_unified_7d_utilization",
    "anthropic-ratelimit-unified-7d-status": "ratelimit_unified_7d_status",
    "anthropic-ratelimit-unified-7d-surpassed-threshold": "ratelimit_unified_7d_surpassed_threshold",
    "anthropic-ratelimit-unified-overage-status": "ratelimit_unified_overage_status",
    "anthropic-ratelimit-unified-fallback-percentage": "ratelimit_unified_fallback_percentage",
}

_STR_KEYS = frozenset(
    k for k in _RATELIMIT_HEADER_MAP.values()
    if k.endswith("_reset") or k.endswith("_status") or k.endswith("_reason")
    or k == "retry_after"
)


def _extract_ratelimit_headers(resp: httpx.Response) -> dict:
    """Normalize Anthropic's per-request and unified rate-limit headers into a flat dict.

    REPO_META capability=gateway.provider.ratelimit-telemetry
    REPO_META purpose="Normalizes Anthropic's per-request and unified rate-limit headers into a flat dict, deriving utilization ratios the dashboard and gateway consume."
    REPO_META reads=data.upstream-response-headers
    REPO_META writes=data.rate-limit-snapshot
    REPO_META role=adapter
    """
    out: dict = {}
    for hdr, key in _RATELIMIT_HEADER_MAP.items():
        raw = resp.headers.get(hdr)
        if raw is None:
            continue
        if key in _STR_KEYS:
            out[key] = raw
        else:
            try:
                out[key] = float(raw) if "." in raw else int(raw)
            except (ValueError, TypeError):
                out[key] = raw

    for bucket in ("tokens", "input_tokens", "output_tokens"):
        limit = out.get(f"ratelimit_{bucket}_limit")
        remaining = out.get(f"ratelimit_{bucket}_remaining")
        if isinstance(limit, (int, float)) and isinstance(remaining, (int, float)) and limit > 0:
            out[f"ratelimit_{bucket}_utilization"] = round(1.0 - remaining / limit, 4)

    # Map unified utilization to the standard tokens_utilization field
    # so the rate_limits table gets populated from either source
    if "ratelimit_tokens_utilization" not in out:
        u5h = out.get("ratelimit_unified_5h_utilization")
        if isinstance(u5h, (int, float)):
            out["ratelimit_tokens_utilization"] = round(u5h / 100, 4)

    return out


class AnthropicBackend(ProviderBackend):
    name = "anthropic"
    _last_ratelimit: dict = {}
    _config_models: list[str] | None = None

    def set_config_models(self, model_ids: list[str]) -> None:
        self._config_models = model_ids

    _STRIP_HEADERS = frozenset({
        "host", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "transfer-encoding",
        "upgrade", "accept-encoding", "content-length", "content-type",
        "authorization", "x-api-key",
        "x-loom-gateway-key", "x-loom-client",
    })

    def _headers(
        self,
        api_key: str,
        inbound_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build upstream request headers, choosing OAuth bearer vs static x-api-key auth.

        REPO_META capability=gateway.provider.auth-headers
        REPO_META purpose="Builds upstream request headers, choosing between OAuth bearer (with the required beta flag) and static x-api-key auth based on the key's shape."
        REPO_META external=service.anthropic
        REPO_META sensitivity=credential
        REPO_META role=adapter
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if inbound_headers:
            for name, value in inbound_headers.items():
                lname = name.lower()
                if lname not in self._STRIP_HEADERS and not lname.startswith("x-loom-"):
                    headers[name] = value
        upstream_host = self.api_base.replace("https://", "").replace("http://", "").split("/")[0]
        headers["host"] = upstream_host
        if api_key:
            if api_key.startswith("sk-ant-oat"):
                headers["Authorization"] = f"Bearer {api_key}"
                beta = headers.get("anthropic-beta", "")
                oauth_flag = "oauth-2025-04-20"
                if oauth_flag not in beta:
                    headers["anthropic-beta"] = f"{beta},{oauth_flag}".lstrip(",")
            else:
                headers["x-api-key"] = api_key
        return headers

    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        api_key: str,
        stream: bool = False,
        inbound_headers: dict[str, str] | None = None,
        query_string: str = "",
        raw_body: dict | None = None,
        **kwargs,
    ) -> dict | AsyncIterator[bytes]:
        """Normalize a chat completion request into Anthropic's Messages API body shape.

        REPO_META capability=gateway.provider.chat-completion
        REPO_META purpose="Normalizes a chat completion request into Anthropic's Messages API body shape and dispatches to the streaming or non-streaming code path."
        REPO_META external=service.anthropic
        REPO_META role=orchestrator
        """
        if raw_body is not None:
            body = dict(raw_body)
            body["model"] = model
            body["messages"] = messages
            body["stream"] = stream
        else:
            body = {"model": model, "messages": messages}
            for key, value in kwargs.items():
                if value is not None:
                    body[key] = value
            body.setdefault("max_tokens", 4096)
            body["stream"] = stream

        if stream:
            return self._stream(body, api_key, inbound_headers, query_string)
        return await self._complete(body, api_key, inbound_headers, query_string)

    async def count_tokens(
        self, body: dict, inbound_headers: dict[str, str]
    ) -> tuple[int, dict]:
        """Forward /v1/messages/count_tokens upstream with passthrough auth.

        Auth headers are relayed as received (x-api-key or Authorization
        bearer) so both API-key and OAuth callers work.

        REPO_META capability=gateway.provider.count-tokens
        REPO_META purpose="Forwards a token-count request to Anthropic's count_tokens endpoint, passing through whichever auth scheme the client used unmodified."
        REPO_META external=service.anthropic
        REPO_META sensitivity=credential
        REPO_META role=adapter
        """
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": inbound_headers.get(
                "anthropic-version", ANTHROPIC_VERSION
            ),
        }
        for name in ("x-api-key", "authorization", "anthropic-beta"):
            value = inbound_headers.get(name)
            if value:
                headers[name] = value
        client = await self.get_client()
        try:
            resp = await client.post(
                "/v1/messages/count_tokens", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc
        return resp.status_code, _safe_json(resp)

    def _upstream_url(self, path: str, query_string: str) -> str:
        return path

    async def _complete(
        self,
        body: dict,
        api_key: str,
        inbound_headers: dict[str, str] | None = None,
        query_string: str = "",
    ) -> dict:
        """Send a non-streaming Messages API request, applying the bounded 429 retry policy.

        REPO_META capability=gateway.provider.chat-completion
        REPO_META purpose="Sends a non-streaming Messages API request, applying the bounded 429 retry policy and capturing rate-limit headers before returning the parsed response."
        REPO_META external=service.anthropic
        REPO_META sensitivity=credential
        REPO_META writes=data.rate-limit-snapshot
        REPO_META role=adapter
        """
        client = await self.get_client()
        url = self._upstream_url("/v1/messages", query_string)
        hdrs = self._headers(api_key, inbound_headers)
        from loom.logging_setup import get_logger
        _log = get_logger("loom.anthropic")
        attempt = 0
        while True:
            req = client.build_request("POST", url, json=body, headers=hdrs)
            _log.info("DEBUG upstream request: url=%s headers=%s",
                      req.url, {k: v for k, v in req.headers.items()
                                if k.lower() not in ("x-api-key", "authorization")})
            try:
                resp = await client.send(req)
            except httpx.HTTPError as exc:
                _log.error(
                    "upstream request failed: url=%s exc_type=%s exc=%s",
                    req.url, type(exc).__name__, exc,
                )
                raise ProviderError(f"anthropic request failed: {exc}") from exc
            self._last_ratelimit = _extract_ratelimit_headers(resp)
            if resp.status_code == 429 and attempt < _MAX_429_RETRIES:
                retry_after = _short_retry_after(resp)
                if retry_after is not None:
                    attempt += 1
                    _log.warning(
                        "upstream 429 — retrying once after %.1fs", retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue
            if resp.status_code >= 400:
                payload = _safe_json(resp)
                _log.error(
                    "upstream %s — headers sent: %s — payload: %s",
                    resp.status_code,
                    {k: v for k, v in self._headers(api_key, inbound_headers).items()
                     if k.lower() != "x-api-key" and k.lower() != "authorization"},
                    payload,
                )
                raise ProviderError(
                    f"anthropic returned {resp.status_code}",
                    status_code=resp.status_code,
                    payload=payload,
                )
            return resp.json()

    async def _stream(
        self,
        body: dict,
        api_key: str,
        inbound_headers: dict[str, str] | None = None,
        query_string: str = "",
    ) -> AsyncIterator[bytes]:
        """Stream a Messages API response, retrying once on a 429 or a pre-data connection failure.

        REPO_META capability=gateway.provider.chat-completion
        REPO_META purpose="Streams a Messages API response chunk-by-chunk, retrying once on a 429 or a connection failure that occurred before any bytes reached the client."
        REPO_META external=service.anthropic
        REPO_META sensitivity=credential
        REPO_META writes=data.rate-limit-snapshot
        REPO_META role=adapter
        """
        client = await self.get_client()
        url = self._upstream_url("/v1/messages", query_string)
        from loom.logging_setup import get_logger
        _log = get_logger("loom.anthropic")
        attempt = 0
        conn_attempt = 0
        while True:
            yielded_any = False
            try:
                async with client.stream(
                    "POST",
                    url,
                    json=body,
                    headers=self._headers(api_key, inbound_headers),
                ) as resp:
                    self._last_ratelimit = _extract_ratelimit_headers(resp)
                    if resp.status_code == 429 and attempt < _MAX_429_RETRIES:
                        retry_after = _short_retry_after(resp)
                        if retry_after is not None:
                            await resp.aread()
                            attempt += 1
                            _log.warning(
                                "upstream stream 429 — retrying once after %.1fs",
                                retry_after,
                            )
                            await asyncio.sleep(retry_after)
                            continue
                    if resp.status_code >= 400:
                        await resp.aread()
                        payload = _safe_json(resp)
                        _log.error(
                            "upstream stream %s — headers sent: %s — payload: %s",
                            resp.status_code,
                            {k: v for k, v in self._headers(api_key, inbound_headers).items()
                             if k.lower() != "x-api-key" and k.lower() != "authorization"},
                            payload,
                        )
                        raise ProviderError(
                            f"anthropic stream returned {resp.status_code}",
                            status_code=resp.status_code,
                            payload=payload,
                        )
                    async for chunk in resp.aiter_raw():
                        if chunk:
                            yielded_any = True
                            yield chunk
                    return
            except httpx.HTTPError as exc:
                if yielded_any or conn_attempt >= _MAX_STREAM_CONN_RETRIES:
                    _log.error(
                        "upstream stream connection failed: exc_type=%s exc=%s "
                        "(yielded_any=%s, conn_attempt=%d)",
                        type(exc).__name__, exc, yielded_any, conn_attempt,
                    )
                    raise ProviderError(f"anthropic stream failed: {exc}") from exc
                conn_attempt += 1
                _log.warning(
                    "upstream stream connection failed before any data — "
                    "retrying once: exc_type=%s exc=%s",
                    type(exc).__name__, exc,
                )
                continue

    async def list_models(self) -> list[str]:
        """Return the configured model allowlist, or a maintained fallback list.

        REPO_META capability=gateway.provider.list-models
        REPO_META purpose="Returns the configured model allowlist if set, otherwise a maintained fallback list, since Anthropic has no public list-models endpoint."
        REPO_META role=adapter
        """
        if self._config_models:
            return list(self._config_models)
        return list(_KNOWN_MODELS)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": resp.text[:500], "type": "provider_error"}}
