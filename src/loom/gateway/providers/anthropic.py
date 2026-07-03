"""Anthropic provider backend.

Targets the Anthropic Messages API (``/v1/messages``). Requests use the
Anthropic-native message format (``role`` + ``content`` blocks) and authenticate
via the ``x-api-key`` header plus a pinned ``anthropic-version``.
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from .base import ProviderBackend, ProviderError


class StreamWithHeaders:
    """Async iterator wrapper that carries upstream response headers."""

    def __init__(self, inner: AsyncIterator[bytes]):
        self._inner = inner
        self.upstream_headers: dict[str, str] = {}

    def __aiter__(self):
        return self._inner.__aiter__()

    async def __anext__(self):
        return await self._inner.__anext__()

ANTHROPIC_VERSION = "2023-06-01"

_CAPTURE_RESPONSE_HEADERS = (
    "x-request-id",
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-limit",
    "anthropic-ratelimit-input-tokens-remaining",
    "anthropic-ratelimit-input-tokens-reset",
    "anthropic-ratelimit-output-tokens-limit",
    "anthropic-ratelimit-output-tokens-remaining",
    "anthropic-ratelimit-output-tokens-reset",
    "retry-after",
    # Unified rate-limit headers (2025+ API)
    "anthropic-ratelimit-unified-status",
    "anthropic-ratelimit-unified-reset",
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-5h-status",
    "anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-7d-status",
)

import re

_MODEL_UTIL_RE = re.compile(
    r"^anthropic-ratelimit-unified-7d_(.+)-(utilization|status)$"
)


def _extract_upstream_headers(resp: httpx.Response) -> dict[str, str]:
    captured = {
        name: resp.headers[name]
        for name in _CAPTURE_RESPONSE_HEADERS
        if name in resp.headers
    }
    # Dynamic per-model headers: anthropic-ratelimit-unified-7d_<model>-utilization
    for name, value in resp.headers.items():
        m = _MODEL_UTIL_RE.match(name.lower())
        if m:
            captured[name.lower()] = value
    return captured

# Anthropic has no public "list models" endpoint, so this is a maintained list.
_KNOWN_MODELS = [
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
]


class AnthropicBackend(ProviderBackend):
    name = "anthropic"

    def _headers(
        self,
        api_key: str,
        *,
        client_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if client_headers:
            if "anthropic-version" in client_headers:
                headers["anthropic-version"] = client_headers["anthropic-version"]
            if "anthropic-beta" in client_headers:
                headers["anthropic-beta"] = client_headers["anthropic-beta"]

        if api_key:
            if api_key.startswith("sk-ant-oat"):
                headers["Authorization"] = f"Bearer {api_key}"
                existing_beta = headers.get("anthropic-beta", "")
                oauth_flag = "oauth-2025-04-20"
                if oauth_flag not in existing_beta:
                    headers["anthropic-beta"] = (
                        f"{existing_beta},{oauth_flag}" if existing_beta else oauth_flag
                    )
            else:
                headers["x-api-key"] = api_key
        return headers

    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        api_key: str,
        stream: bool = False,
        client_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict | AsyncIterator[bytes]:
        body: dict = {"model": model, "messages": messages}
        for key, value in kwargs.items():
            if value is not None:
                body[key] = value
        # Anthropic requires max_tokens; supply a default if the client omitted it.
        body.setdefault("max_tokens", 4096)
        body["stream"] = stream

        if stream:
            return await self._stream(body, api_key, client_headers=client_headers)
        return await self._complete(body, api_key, client_headers=client_headers)

    async def count_tokens(
        self, body: dict, inbound_headers: dict[str, str]
    ) -> tuple[int, dict]:
        """Forward /v1/messages/count_tokens upstream with passthrough auth.

        Auth headers are relayed as received (x-api-key or Authorization
        bearer) so both API-key and OAuth callers work.
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

    async def _complete(
        self,
        body: dict,
        api_key: str,
        *,
        client_headers: dict[str, str] | None = None,
    ) -> dict:
        client = await self.get_client()
        headers = self._headers(api_key, client_headers=client_headers)
        try:
            resp = await client.post(
                "/v1/messages", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"anthropic returned {resp.status_code}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )
        result = resp.json()
        result["_upstream_headers"] = _extract_upstream_headers(resp)
        return result

    async def _stream(
        self,
        body: dict,
        api_key: str,
        *,
        client_headers: dict[str, str] | None = None,
    ) -> StreamWithHeaders:
        client = await self.get_client()
        headers = self._headers(api_key, client_headers=client_headers)

        async def _iter() -> AsyncIterator[bytes]:
            async with client.stream(
                "POST", "/v1/messages", json=body, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ProviderError(
                        f"anthropic stream returned {resp.status_code}",
                        status_code=resp.status_code,
                        payload=_safe_json(resp),
                    )
                wrapper.upstream_headers = _extract_upstream_headers(resp)
                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk

        wrapper = StreamWithHeaders(_iter())
        return wrapper

    async def list_models(self) -> list[str]:
        return list(_KNOWN_MODELS)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": resp.text[:500], "type": "provider_error"}}
