"""Google Gemini provider backend.

Targets the Gemini Generative Language API
(``/models/{model}:generateContent``). The configured ``api_base`` is expected
to include the version segment, e.g.
``https://generativelanguage.googleapis.com/v1beta``.

Gemini's wire format differs from OpenAI's: messages live in a ``contents``
array with ``role`` values ``user``/``model``, text is wrapped in a ``parts``
array, the system prompt is hoisted into a separate ``system_instruction``
field, and sampling/length parameters are nested under ``generationConfig``.
This backend translates OpenAI-style inputs into that shape and returns the raw
Gemini response dict; the gateway normalizes responses separately.

Authentication uses the ``x-goog-api-key`` header. Streaming uses the
``:streamGenerateContent?alt=sse`` endpoint, which emits SSE chunks forwarded
verbatim.
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from .base import ProviderBackend, ProviderError

# Gemini has no stable per-key default list, so this is a maintained fallback.
_KNOWN_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
]

# OpenAI generation params -> generationConfig keys.
_GENERATION_CONFIG_MAP = {
    "temperature": "temperature",
    "max_tokens": "maxOutputTokens",
    "top_p": "topP",
    "top_k": "topK",
    "stop": "stopSequences",
}


class GeminiBackend(ProviderBackend):
    name = "gemini"

    def _headers(self, api_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["x-goog-api-key"] = api_key
        return headers

    @staticmethod
    def _content_to_parts(content) -> list[dict]:
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in (None, "text"):
                    parts.append({"text": block.get("text", "")})
            return parts
        return [{"text": content if isinstance(content, str) else str(content)}]

    @classmethod
    def _to_gemini(cls, messages: list[dict]) -> tuple[list[dict], dict | None]:
        """Split OpenAI messages into Gemini ``contents`` + ``system_instruction``."""
        contents: list[dict] = []
        system_parts: list[dict] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_parts.extend(cls._content_to_parts(content))
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append(
                {"role": gemini_role, "parts": cls._content_to_parts(content)}
            )
        system_instruction = {"parts": system_parts} if system_parts else None
        return contents, system_instruction

    @staticmethod
    def _generation_config(kwargs: dict) -> dict:
        config: dict = {}
        for src, dest in _GENERATION_CONFIG_MAP.items():
            if kwargs.get(src) is not None:
                config[dest] = kwargs[src]
        return config

    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        api_key: str,
        stream: bool = False,
        **kwargs,
    ) -> dict | AsyncIterator[bytes]:
        contents, system_instruction = self._to_gemini(messages)
        body: dict = {"contents": contents}
        if system_instruction is not None:
            body["system_instruction"] = system_instruction
        generation_config = self._generation_config(kwargs)
        if generation_config:
            body["generationConfig"] = generation_config

        if stream:
            return self._stream(model, body, api_key)
        return await self._complete(model, body, api_key)

    async def _complete(self, model: str, body: dict, api_key: str) -> dict:
        client = await self.get_client()
        try:
            resp = await client.post(
                f"/models/{model}:generateContent",
                json=body,
                headers=self._headers(api_key),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"gemini returned {resp.status_code}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )
        return resp.json()

    async def _stream(
        self, model: str, body: dict, api_key: str
    ) -> AsyncIterator[bytes]:
        client = await self.get_client()
        async with client.stream(
            "POST",
            f"/models/{model}:streamGenerateContent?alt=sse",
            json=body,
            headers=self._headers(api_key),
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise ProviderError(
                    f"gemini stream returned {resp.status_code}",
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
            return list(_KNOWN_MODELS)
        data = resp.json()
        models = [
            m.get("name", "").removeprefix("models/")
            for m in data.get("models", [])
            if m.get("name")
        ]
        return models or list(_KNOWN_MODELS)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": resp.text[:500], "type": "provider_error"}}
