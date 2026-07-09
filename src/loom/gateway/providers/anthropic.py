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
}


def _extract_ratelimit_headers(resp: httpx.Response) -> dict:
    out: dict = {}
    for hdr, key in _RATELIMIT_HEADER_MAP.items():
        raw = resp.headers.get(hdr)
        if raw is None:
            out[key] = None
            continue
        if key.endswith("_reset") or key == "retry_after":
            out[key] = raw
        else:
            try:
                out[key] = int(raw)
            except (ValueError, TypeError):
                out[key] = raw

    for bucket in ("tokens", "input_tokens", "output_tokens"):
        limit = out.get(f"ratelimit_{bucket}_limit")
        remaining = out.get(f"ratelimit_{bucket}_remaining")
        if isinstance(limit, int) and isinstance(remaining, int) and limit > 0:
            out[f"ratelimit_{bucket}_utilization"] = round(1.0 - remaining / limit, 4)
        else:
            out[f"ratelimit_{bucket}_utilization"] = None

    return out


class AnthropicBackend(ProviderBackend):
    name = "anthropic"
    _last_ratelimit: dict = {}

    _STRIP_HEADERS = frozenset({
        "host", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "transfer-encoding",
        "upgrade", "accept-encoding", "content-length", "content-type",
        "authorization", "x-api-key",
    })

    def _headers(
        self,
        api_key: str,
        inbound_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if inbound_headers:
            for name, value in inbound_headers.items():
                if name.lower() not in self._STRIP_HEADERS:
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
        client = await self.get_client()
        url = self._upstream_url("/v1/messages", query_string)
        hdrs = self._headers(api_key, inbound_headers)
        import logging
        _log = logging.getLogger("loom.anthropic")
        req = client.build_request("POST", url, json=body, headers=hdrs)
        _log.info("DEBUG upstream request: url=%s headers=%s",
                  req.url, {k: v for k, v in req.headers.items()
                            if k.lower() not in ("x-api-key", "authorization")})
        try:
            resp = await client.send(req)
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc
        self._last_ratelimit = _extract_ratelimit_headers(resp)
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
        client = await self.get_client()
        url = self._upstream_url("/v1/messages", query_string)
        async with client.stream(
            "POST",
            url,
            json=body,
            headers=self._headers(api_key, inbound_headers),
        ) as resp:
            self._last_ratelimit = _extract_ratelimit_headers(resp)
            if resp.status_code >= 400:
                await resp.aread()
                payload = _safe_json(resp)
                import logging
                logging.getLogger("loom.anthropic").error(
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
                    yield chunk

    async def list_models(self) -> list[str]:
        return list(_KNOWN_MODELS)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": resp.text[:500], "type": "provider_error"}}
