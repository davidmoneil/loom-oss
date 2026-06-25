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
        body: dict = {"model": model, "messages": messages}
        for key, value in kwargs.items():
            if value is not None:
                body[key] = value
        body["stream"] = stream

        if stream:
            return self._stream(body, api_key)
        return await self._complete(body, api_key)

    async def _complete(self, body: dict, api_key: str) -> dict:
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
