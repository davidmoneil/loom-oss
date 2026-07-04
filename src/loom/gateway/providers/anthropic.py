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

    def _headers(
        self,
        api_key: str,
        inbound_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if inbound_headers:
            if "anthropic-version" in inbound_headers:
                headers["anthropic-version"] = inbound_headers["anthropic-version"]
            if "anthropic-beta" in inbound_headers:
                headers["anthropic-beta"] = inbound_headers["anthropic-beta"]
        if api_key:
            if api_key.startswith("sk-ant-oat"):
                headers["Authorization"] = f"Bearer {api_key}"
                existing_beta = headers.get("anthropic-beta", "")
                oauth_beta = "oauth-2025-04-20"
                if oauth_beta not in existing_beta:
                    headers["anthropic-beta"] = (
                        f"{existing_beta},{oauth_beta}" if existing_beta else oauth_beta
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
        inbound_headers: dict[str, str] | None = None,
        query_string: str = "",
        **kwargs,
    ) -> dict | AsyncIterator[bytes]:
        body: dict = {"model": model, "messages": messages}
        for key, value in kwargs.items():
            if value is not None:
                body[key] = value
        # Anthropic requires max_tokens; supply a default if the client omitted
        # it — but not when extended thinking is enabled (thinking sets its own
        # budget and max_tokens is handled differently).
        if "thinking" not in body:
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
        inbound_headers: dict[str, str] | None = None,
        query_string: str = "",
    ) -> dict:
        client = await self.get_client()
        url = f"/v1/messages?{query_string}" if query_string else "/v1/messages"
        try:
            resp = await client.post(
                url, json=body, headers=self._headers(api_key, inbound_headers)
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

    async def _stream(
        self,
        body: dict,
        api_key: str,
        inbound_headers: dict[str, str] | None = None,
        query_string: str = "",
    ) -> AsyncIterator[bytes]:
        client = await self.get_client()
        url = f"/v1/messages?{query_string}" if query_string else "/v1/messages"
        async with client.stream(
            "POST", url, json=body, headers=self._headers(api_key, inbound_headers)
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
