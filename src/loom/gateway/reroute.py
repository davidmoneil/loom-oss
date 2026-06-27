"""Cloud backend rerouting for Ollama-format requests.

When the routing engine decides a local model can't handle a task,
transparently reroutes to a cloud provider and wraps the response
in Ollama-format envelopes so the caller sees a seamless response.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .providers.base import ProviderBackend


def _ollama_generate_envelope(
    model: str,
    response_text: str,
    total_duration_ns: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    routed_from: str | None = None,
) -> dict:
    result = {
        "model": model,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "response": response_text,
        "done": True,
        "done_reason": "stop",
        "total_duration": total_duration_ns,
        "prompt_eval_count": prompt_tokens,
        "prompt_eval_duration": 0,
        "eval_count": completion_tokens,
        "eval_duration": total_duration_ns,
        "load_duration": 0,
    }
    if routed_from:
        result["loom_routed_from"] = routed_from
    return result


def _ollama_chat_envelope(
    model: str,
    response_text: str,
    total_duration_ns: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    routed_from: str | None = None,
) -> dict:
    result = {
        "model": model,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "message": {"role": "assistant", "content": response_text},
        "done": True,
        "done_reason": "stop",
        "total_duration": total_duration_ns,
        "prompt_eval_count": prompt_tokens,
        "prompt_eval_duration": 0,
        "eval_count": completion_tokens,
        "eval_duration": total_duration_ns,
        "load_duration": 0,
    }
    if routed_from:
        result["loom_routed_from"] = routed_from
    return result


async def reroute_to_cloud(
    backend: ProviderBackend,
    model: str,
    api_key: str,
    messages: list[dict],
    original_model: str,
    endpoint: str = "chat",
    **kwargs,
) -> dict:
    """Execute a request via a cloud backend, return Ollama-format envelope."""
    start = time.monotonic()

    result = await backend.chat_completion(
        model=model,
        messages=messages,
        api_key=api_key,
        stream=False,
        **kwargs,
    )

    elapsed_ns = int((time.monotonic() - start) * 1e9)

    text = ""
    usage = result.get("usage", {})
    prompt_tokens = 0
    completion_tokens = 0

    provider = getattr(backend, "name", "unknown")

    if provider == "anthropic":
        content_blocks = result.get("content", [])
        if isinstance(content_blocks, list):
            text = "".join(
                b.get("text", "") for b in content_blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
        usage = result.get("usage", {})
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)
    else:
        choices = result.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

    envelope_fn = _ollama_chat_envelope if endpoint == "chat" else _ollama_generate_envelope
    return envelope_fn(
        model=original_model,
        response_text=text,
        total_duration_ns=elapsed_ns,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        routed_from=original_model if original_model != model else None,
    )
