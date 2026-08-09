"""Abstract base class for LLM provider backends.

A backend is a thin async HTTP client that knows how to talk to one provider's
wire format. The gateway holds one backend instance per configured provider and
forwards client requests through it. API keys are supplied per-request (pulled
from the inbound client headers) — backends never read keys from server config.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

import httpx


class ProviderError(Exception):
    """Raised when an upstream provider returns an error or is unreachable.

    Carries an HTTP ``status_code`` and a ``payload`` dict suitable for returning
    to the client as a JSON error body.
    """

    def __init__(self, message: str, status_code: int = 502, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {
            "error": {"message": message, "type": "provider_error"}
        }


class ProviderBackend(ABC):
    """Base class for LLM provider backends."""

    #: Short provider identifier (set by subclasses), e.g. "openai".
    name: str = "base"

    def __init__(self, api_base: str):
        self.api_base = api_base.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.api_base,
                timeout=httpx.Timeout(120.0, connect=120.0, read=120.0, write=120.0, pool=120.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @abstractmethod
    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        api_key: str,
        stream: bool = False,
        **kwargs,
    ) -> dict | AsyncIterator[bytes]:
        """Send a chat completion request.

        Returns a parsed response dict when ``stream`` is False, or an async
        iterator yielding raw upstream bytes when ``stream`` is True.
        """
        ...

    @abstractmethod
    async def list_models(self) -> list[str]:
        """List available model IDs from this provider."""
        ...
