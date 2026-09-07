"""OpenAI-compatible provider backend.

Targets the OpenAI Chat Completions API (``/chat/completions``). Because the
wire format is a de-facto standard, this backend also serves any
OpenAI-compatible endpoint (Together, Groq, vLLM, LM Studio, etc.). The
configured ``api_base`` is expected to already include the ``/v1`` segment when
required (e.g. ``https://api.openai.com/v1``).
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from .base import ProviderBackend, ProviderError


class OpenAIBackend(ProviderBackend):
    name = "openai"

    def _headers(self, api_key: str) -> dict[str, str]:
        """Build the bearer-token Authorization header OpenAI-compatible endpoints expect.

        REPO_META capability=gateway.provider.auth-headers
        REPO_META purpose="Builds the bearer-token Authorization header OpenAI-compatible endpoints expect."
        REPO_META external=service.openai
        REPO_META sensitivity=credential
        REPO_META role=adapter
        """
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        api_key: str,
        stream: bool = False,
        **kwargs,
    ) -> dict | AsyncIterator[bytes]:
        """Normalize a chat completion request into the OpenAI Chat Completions body shape.

        REPO_META capability=gateway.provider.chat-completion
        REPO_META purpose="Normalizes a chat completion request into the OpenAI Chat Completions body shape and dispatches to the streaming or non-streaming code path."
        REPO_META external=service.openai
        REPO_META role=orchestrator
        """
        body: dict = {"model": model, "messages": messages}
        for key, value in kwargs.items():
            if value is not None:
                body[key] = value
        body["stream"] = stream

        if stream:
            return self._stream(body, api_key)
        return await self._complete(body, api_key)

    async def _complete(self, body: dict, api_key: str) -> dict:
        """Send a non-streaming request to the OpenAI-compatible /chat/completions endpoint.

        REPO_META capability=gateway.provider.chat-completion
        REPO_META purpose="Sends a non-streaming request to the OpenAI-compatible /chat/completions endpoint and raises a provider error on a non-2xx response."
        REPO_META external=service.openai
        REPO_META sensitivity=credential
        REPO_META role=adapter
        """
        client = await self.get_client()
        try:
            resp = await client.post(
                "/chat/completions", json=body, headers=self._headers(api_key)
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"openai returned {resp.status_code}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )
        return resp.json()

    async def _stream(self, body: dict, api_key: str) -> AsyncIterator[bytes]:
        """Stream a Chat Completions response chunk-by-chunk from an OpenAI-compatible endpoint.

        REPO_META capability=gateway.provider.chat-completion
        REPO_META purpose="Streams a Chat Completions response chunk-by-chunk from an OpenAI-compatible endpoint."
        REPO_META external=service.openai
        REPO_META sensitivity=credential
        REPO_META role=adapter
        """
        client = await self.get_client()
        async with client.stream(
            "POST", "/chat/completions", json=body, headers=self._headers(api_key)
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise ProviderError(
                    f"openai stream returned {resp.status_code}",
                    status_code=resp.status_code,
                    payload=_safe_json(resp),
                )
            async for chunk in resp.aiter_raw():
                if chunk:
                    yield chunk

    async def list_models(self) -> list[str]:
        """Query the provider's /models endpoint and return the reported model IDs.

        REPO_META capability=gateway.provider.list-models
        REPO_META purpose="Queries the provider's /models endpoint and returns the reported model IDs, or an empty list if the endpoint call fails."
        REPO_META external=service.openai
        REPO_META role=adapter
        """
        client = await self.get_client()
        try:
            resp = await client.get("/models")
            resp.raise_for_status()
        except httpx.HTTPError:
            return []
        data = resp.json()
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": resp.text[:500], "type": "provider_error"}}
