"""Anthropic provider backend.

Targets the Anthropic Messages API (``/v1/messages``). Requests use the
Anthropic-native message format (``role`` + ``content`` blocks) and authenticate
via the ``x-api-key`` header plus a pinned ``anthropic-version``.
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from .base import ProviderBackend, ProviderError

ANTHROPIC_VERSION = "2023-06-01"

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

    def _headers(self, api_key: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if api_key:
            # OAuth access tokens (sk-ant-oat...) authenticate via
            # Authorization: Bearer, not x-api-key — interactive Claude Code
            # sessions use these. API keys keep the x-api-key header.
            if api_key.startswith("sk-ant-oat"):
                headers["Authorization"] = f"Bearer {api_key}"
                headers["anthropic-beta"] = "oauth-2025-04-20"
            else:
                headers["x-api-key"] = api_key
        return headers

    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        api_key: str,
        stream: bool = False,
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
            return self._stream(body, api_key)
        return await self._complete(body, api_key)

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

    async def _complete(self, body: dict, api_key: str) -> dict:
        client = await self.get_client()
        try:
            resp = await client.post(
                "/v1/messages", json=body, headers=self._headers(api_key)
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"anthropic returned {resp.status_code}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )
        return resp.json()

    async def _stream(self, body: dict, api_key: str) -> AsyncIterator[bytes]:
        client = await self.get_client()
        async with client.stream(
            "POST", "/v1/messages", json=body, headers=self._headers(api_key)
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise ProviderError(
                    f"anthropic stream returned {resp.status_code}",
                    status_code=resp.status_code,
                    payload=_safe_json(resp),
                )
            async for chunk in resp.aiter_raw():
                if chunk:
                    yield chunk

    async def list_models(self) -> list[str]:
        return list(_KNOWN_MODELS)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": resp.text[:500], "type": "provider_error"}}
