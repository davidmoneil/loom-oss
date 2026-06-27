"""Ollama provider backend.

Targets a local Ollama server's chat API (``/api/chat``). OpenAI-style messages
map directly onto Ollama's ``{role, content}`` message shape. Ollama streams
NDJSON (one JSON object per line) rather than SSE; the gateway forwards those
lines verbatim.
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from .base import ProviderBackend, ProviderError

# Ollama runs locally and needs no key; long timeout for cold model loads.
_OLLAMA_TIMEOUT = 300.0


class OllamaBackend(ProviderBackend):
    name = "ollama"

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.api_base, timeout=_OLLAMA_TIMEOUT)
        return self._client

    @staticmethod
    def _to_ollama_messages(messages: list[dict]) -> list[dict]:
        converted: list[dict] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                # Flatten OpenAI/Anthropic content-block arrays to plain text.
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") in (None, "text")
                ]
                content = "".join(parts)
            converted.append({"role": msg.get("role", "user"), "content": content})
        return converted

    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        api_key: str,
        stream: bool = False,
        **kwargs,
    ) -> dict | AsyncIterator[bytes]:
        options: dict = {}
        for key in ("temperature", "top_p", "top_k", "seed", "num_predict"):
            if kwargs.get(key) is not None:
                options[key] = kwargs[key]
        if kwargs.get("max_tokens") is not None:
            options.setdefault("num_predict", kwargs["max_tokens"])

        body: dict = {
            "model": model,
            "messages": self._to_ollama_messages(messages),
            "stream": stream,
        }
        if options:
            body["options"] = options

        if stream:
            return self._stream(body)
        return await self._complete(body)

    async def _complete(self, body: dict) -> dict:
        client = await self.get_client()
        try:
            resp = await client.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"ollama returned {resp.status_code}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )
        return resp.json()

    async def _stream(self, body: dict) -> AsyncIterator[bytes]:
        client = await self.get_client()
        async with client.stream("POST", "/api/chat", json=body) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise ProviderError(
                    f"ollama stream returned {resp.status_code}",
                    status_code=resp.status_code,
                    payload=_safe_json(resp),
                )
            async for line in resp.aiter_lines():
                if line:
                    yield (line + "\n").encode("utf-8")

    async def generate(
        self,
        model: str,
        prompt: str,
        stream: bool = False,
        **kwargs,
    ) -> dict | AsyncIterator[bytes]:
        options: dict = {}
        for key in ("temperature", "top_p", "top_k", "seed", "num_predict"):
            if kwargs.get(key) is not None:
                options[key] = kwargs[key]
        if kwargs.get("max_tokens") is not None:
            options.setdefault("num_predict", kwargs["max_tokens"])

        body: dict = {"model": model, "prompt": prompt, "stream": stream}
        if options:
            body["options"] = options

        if stream:
            return self._stream_generate(body)

        client = await self.get_client()
        try:
            resp = await client.post("/api/generate", json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama generate failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"ollama returned {resp.status_code}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )
        return resp.json()

    async def _stream_generate(self, body: dict) -> AsyncIterator[bytes]:
        client = await self.get_client()
        async with client.stream("POST", "/api/generate", json=body) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise ProviderError(
                    f"ollama generate stream returned {resp.status_code}",
                    status_code=resp.status_code,
                    payload=_safe_json(resp),
                )
            async for line in resp.aiter_lines():
                if line:
                    yield (line + "\n").encode("utf-8")

    async def list_models(self) -> list[str]:
        client = await self.get_client()
        try:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError:
            return []
        data = resp.json()
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    async def list_models_full(self) -> dict:
        client = await self.get_client()
        try:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError:
            return {"models": []}
        return resp.json()

    async def show_model(self, name: str) -> dict:
        client = await self.get_client()
        try:
            resp = await client.post("/api/show", json={"name": name})
            resp.raise_for_status()
        except httpx.HTTPError:
            return {}
        return resp.json()

    async def get_loaded_models(self) -> list[str]:
        client = await self.get_client()
        try:
            resp = await client.get("/api/ps")
            resp.raise_for_status()
        except httpx.HTTPError:
            return []
        data = resp.json()
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": resp.text[:500], "type": "provider_error"}}
