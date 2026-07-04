"""Loom gateway — FastAPI application.

A transparent proxy that speaks the OpenAI and Anthropic wire formats and
forwards requests to upstream providers, adding three optimizations along the
way:

* **Routing** — when a client requests ``model="auto"`` the routing engine picks
  a concrete model based on the inferred task type and the caller's source
  policy.
* **Compression** — graduated content compression is exposed at ``/v1/compress``.
* **Detection** — determinism/tier detection is exposed at ``/v1/detect``.

API keys are *pass-through*: they are read from the inbound request headers and
forwarded to the upstream provider. The server never stores provider keys.

Sibling engine modules (routing, detection, compression, observability) are
imported defensively — if one is unavailable the gateway still boots and the
affected feature degrades gracefully rather than crashing the process.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from loom import __version__
from loom.config import LoomConfig, ModelConfig, SourcePolicy
from loom.storage import LoomStorage, create_storage

from .providers import (
    AnthropicBackend,
    GeminiBackend,
    OllamaBackend,
    OpenAIBackend,
    ProviderBackend,
    ProviderError,
)

# --- Optional engines (owned by sibling agents) -----------------------------
# Imported defensively so a missing/in-progress module cannot stop the gateway.
try:
    from loom.observability import AuditLogger  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    AuditLogger = None  # type: ignore

try:
    from loom.routing.engine import RoutingEngine  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    RoutingEngine = None  # type: ignore

try:
    from loom.detection.engine import DetectionEngine  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    DetectionEngine = None  # type: ignore

try:
    from loom.compression.processor import ContentProcessor  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    ContentProcessor = None  # type: ignore

try:
    from loom.compression.relevance import record_request_content  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    record_request_content = None  # type: ignore

try:
    from loom.scanner import SensitiveDataScanner  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    SensitiveDataScanner = None  # type: ignore

try:
    from loom.governor import GovernorValidationError, ThrottleGovernor  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    ThrottleGovernor = None  # type: ignore
    GovernorValidationError = None  # type: ignore

try:
    from loom.compression.tiers import resolve_tier, strip_loom_tag, add_loom_tag, content_hash  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    resolve_tier = None  # type: ignore

try:
    from loom.routing.programmatic_search import get_search_tier  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    get_search_tier = None  # type: ignore

try:
    from loom.gateway.reroute import reroute_to_cloud, _ollama_generate_envelope, _ollama_chat_envelope  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    reroute_to_cloud = None  # type: ignore


# --------------------------------------------------------------------------- #
#  Application state
# --------------------------------------------------------------------------- #
class GatewayState:
    """Holds long-lived objects created at startup and reused per request."""

    def __init__(self) -> None:
        self.started_at: float = time.time()
        self.request_count: int = 0
        self.error_count: int = 0
        # Cumulative compression rollup (estimated tokens) for /health.
        self.comp_tokens_before: int = 0
        self.comp_tokens_after: int = 0
        # Cumulative thinking-block stripping stats.
        self.thinking_blocks_stripped: int = 0
        self.thinking_bytes_saved: int = 0
        self.config: LoomConfig = LoomConfig()
        self.backends: dict[str, ProviderBackend] = {}
        # model id / display name -> (provider_name, ModelConfig)
        self.model_index: dict[str, tuple[str, ModelConfig]] = {}
        self.storage: Optional[LoomStorage] = None
        self.audit: Any = None
        self.routing: Any = None
        self.detection: Any = None
        self.compression: Any = None
        self.scanner: Any = None
        self.governor: Any = None

    def build_backend(self, provider_name: str, api_base: str) -> ProviderBackend:
        name = provider_name.lower()
        if name == "anthropic":
            return AnthropicBackend(api_base)
        if name == "gemini":
            return GeminiBackend(api_base)
        if name == "ollama":
            return OllamaBackend(api_base)
        # Everything else is treated as OpenAI-compatible.
        return OpenAIBackend(api_base)

    def index_models(self) -> None:
        self.model_index.clear()
        for provider in self.config.providers:
            for model in provider.models:
                self.model_index[model.model_id] = (provider.name, model)
                if model.display_name:
                    self.model_index.setdefault(
                        model.display_name, (provider.name, model)
                    )

    def resolve_provider(self, model: str) -> Optional[tuple[str, ModelConfig]]:
        """Map a requested model to (provider_name, ModelConfig).

        Falls back to name-prefix inference, then to the sole configured
        provider, so a proxied model that isn't explicitly listed can still be
        forwarded.
        """
        if model in self.model_index:
            return self.model_index[model]

        lowered = model.lower()
        guess: Optional[str] = None
        if lowered.startswith("claude") or "anthropic" in lowered:
            guess = "anthropic"
        elif lowered.startswith(("gpt", "o1", "o3", "o4", "text-")):
            guess = "openai"
        elif lowered.startswith("gemini"):
            guess = "gemini"
        elif lowered.startswith("grok"):
            guess = "xai"
        if guess:
            for provider in self.config.providers:
                if provider.name == guess:
                    return provider.name, ModelConfig(model_id=model, display_name=model)

        if len(self.config.providers) == 1:
            only = self.config.providers[0]
            return only.name, ModelConfig(model_id=model, display_name=model)
        return None


def _extract_tokens(usage_or_response: Any) -> tuple[int, int]:
    """Pull (tokens_in, tokens_out) from any provider's response shape.

    Handles: OpenAI (usage.prompt_tokens), Anthropic (usage.input_tokens),
    Ollama (top-level prompt_eval_count), Gemini (usageMetadata.promptTokenCount).
    """
    if not isinstance(usage_or_response, dict):
        return 0, 0
    d = usage_or_response
    tokens_in = (
        d.get("prompt_tokens")
        or d.get("input_tokens")
        or d.get("prompt_eval_count")  # Ollama
        or (d.get("usageMetadata") or {}).get("promptTokenCount")  # Gemini
        or 0
    )
    tokens_out = (
        d.get("completion_tokens")
        or d.get("output_tokens")
        or d.get("eval_count")  # Ollama
        or (d.get("usageMetadata") or {}).get("candidatesTokenCount")  # Gemini
        or 0
    )
    try:
        return int(tokens_in or 0), int(tokens_out or 0)
    except (TypeError, ValueError):
        return 0, 0


def _record_request(
    state: GatewayState,
    *,
    request_id: str,
    method: str,
    path: str,
    source: str,
    provider: str,
    model: str,
    requested_model: Optional[str],
    task_type: str,
    routing_reason: str,
    status_code: int,
    latency_ms: float,
    usage: Any = None,
    cost: float = 0.0,
    messages: Optional[list[dict]] = None,
    response_text: Optional[str] = None,
    compressed: bool = False,
    compression_ratio: float = 1.0,
) -> None:
    """Persist + audit a completed request. Never raises into the request path."""
    tokens_in, tokens_out = _extract_tokens(usage)

    if state.storage is not None:
        try:
            state.storage.record_metrics(
                request_id=request_id,
                model=model,
                requested_model=requested_model,
                provider=provider,
                task_type=task_type,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost=cost,
                source=source,
                compressed=compressed,
                compression_ratio=compression_ratio,
            )
        except Exception:
            pass
        try:
            state.storage.record_routing_decision(
                request_id=request_id,
                source=source,
                task_type=task_type,
                model=model,
                reason=routing_reason,
                model_recommended=requested_model,
            )
        except Exception:
            pass

    if state.audit is not None:
        try:
            state.audit.log_request(
                request_id=request_id,
                method=method,
                path=path,
                source=source,
                model=model,
                requested_model=requested_model,
                provider=provider,
                task_type=task_type,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost_estimate=cost,
                routing_reason=routing_reason,
                status_code=status_code,
            )
        except Exception:
            pass
        try:
            state.audit.log_metrics(
                request_id=request_id,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost_estimate=cost,
            )
        except Exception:
            pass

    if (
        record_request_content is not None
        and messages
        and status_code == 200
        and state.storage is not None
    ):
        try:
            record_request_content(messages, state.storage, source=source)
        except Exception:
            pass

    if state.audit is not None and messages and state.scanner is not None:
        try:
            state.audit.log_content(
                request_id=request_id,
                model=model,
                source=source,
                provider=provider,
                messages=messages,
                response_text=response_text,
                content_logging=state.scanner.content_logging,
            )
        except Exception:
            pass


def _audit_error(state: GatewayState, request_id: str, path: str, source: str, status_code: int) -> None:
    if state.audit is None:
        return
    try:
        state.audit.log_request(
            request_id=request_id,
            method="POST",
            path=path,
            source=source,
            status_code=status_code,
        )
    except Exception:
        pass


def _extract_usage(result: Any, provider: str) -> dict:
    """Extract a normalized usage dict from any provider's response."""
    if not isinstance(result, dict):
        return {}
    if provider == "ollama":
        return {
            "prompt_tokens": result.get("prompt_eval_count", 0),
            "completion_tokens": result.get("eval_count", 0),
        }
    if provider == "gemini":
        um = result.get("usageMetadata", {})
        return {
            "prompt_tokens": um.get("promptTokenCount", 0),
            "completion_tokens": um.get("candidatesTokenCount", 0),
        }
    # OpenAI / Anthropic both put usage in a sub-dict
    return result.get("usage") or {}


def _normalize_response(result: dict, provider: str, model: str) -> dict:
    """Normalize any provider's response to OpenAI chat completion format."""
    if provider == "ollama":
        msg = result.get("message", {})
        content = msg.get("content", "")
        return {
            "id": f"chatcmpl-loom-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop" if result.get("done") else None,
                }
            ],
            "usage": {
                "prompt_tokens": result.get("prompt_eval_count", 0),
                "completion_tokens": result.get("eval_count", 0),
                "total_tokens": (result.get("prompt_eval_count", 0)
                                 + result.get("eval_count", 0)),
            },
        }
    if provider == "gemini":
        candidates = result.get("candidates", [{}])
        text = ""
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
        um = result.get("usageMetadata", {})
        return {
            "id": f"chatcmpl-loom-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": um.get("promptTokenCount", 0),
                "completion_tokens": um.get("candidatesTokenCount", 0),
                "total_tokens": um.get("totalTokenCount", 0),
            },
        }
    if provider == "anthropic":
        content_blocks = result.get("content", [])
        text = ""
        if isinstance(content_blocks, list):
            text = "".join(
                b.get("text", "") for b in content_blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
        usage = result.get("usage", {})
        return {
            "id": f"chatcmpl-loom-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": result.get("stop_reason", "stop"),
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": (
                    usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                ),
            },
        }
    # OpenAI responses are already in standard format
    return result


def _model_cost(model_cfg: Optional[ModelConfig], usage: Any) -> float:
    if model_cfg is None:
        return 0.0
    tokens_in, tokens_out = _extract_tokens(usage)
    return (tokens_in / 1000.0) * model_cfg.cost_per_1k_input + (
        tokens_out / 1000.0
    ) * model_cfg.cost_per_1k_output


# --------------------------------------------------------------------------- #
#  Routing helpers
# --------------------------------------------------------------------------- #
def _classify_task_type(body: dict, messages: list[dict]) -> str:
    """Lightweight task-type heuristic used to seed routing decisions."""
    if body.get("tools") or body.get("functions"):
        return "tool_use"
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in (
        "json_object",
        "json_schema",
    ):
        return "json_generation"
    blob = " ".join(
        str(m.get("content", "")) for m in messages if isinstance(m, dict)
    ).lower()
    if "json" in blob:
        return "json_generation"
    if any(k in blob for k in ("story", "poem", "creative", "imagine")):
        return "story_generation"
    return "general"


def _recommendation_model(rec: Any) -> Optional[str]:
    if rec is None:
        return None
    if isinstance(rec, str):
        return rec
    for attr in ("model_id", "model", "recommended_model"):
        value = getattr(rec, attr, None)
        if value:
            return value
    if isinstance(rec, dict):
        return rec.get("model_id") or rec.get("model")
    return None


def _fallback_model(config: LoomConfig, policy: SourcePolicy) -> Optional[str]:
    """Pick a model directly from config when routing is unavailable."""
    allowed = set(policy.allowed_providers or [])
    for provider in config.providers:
        if allowed and provider.name not in allowed:
            continue
        for model in provider.models:
            if policy.requires_tools and not model.supports_tools:
                continue
            return model.model_id
    # Last resort: anything at all.
    for provider in config.providers:
        if provider.models:
            return provider.models[0].model_id
    return None


def _select_model(
    state: GatewayState,
    requested_model: Optional[str],
    source: str,
    body: dict,
    messages: list[dict],
) -> tuple[str, str, str]:
    """Resolve the model to use. Returns (model, task_type, routing_reason)."""
    policy = state.config.get_source_policy(source)
    task_type = _classify_task_type(body, messages)

    explicit = requested_model not in (None, "", "auto", "loom-auto")
    if explicit:
        return requested_model, task_type, "client_specified"  # type: ignore[return-value]

    if policy.pinned_model:
        return policy.pinned_model, task_type, "source_pinned"

    if state.routing is not None:
        rec = _try_recommend(state.routing, task_type, source, policy)
        model = _recommendation_model(rec)
        if model:
            reason = getattr(rec, "routing_reason", "") or "routed"
            return model, task_type, reason

    fallback = _fallback_model(state.config, policy)
    if fallback:
        return fallback, task_type, "config_fallback"
    # Nothing configured — surface a clear error upstream.
    raise ProviderError(
        "no model available: routing produced no result and no providers are configured",
        status_code=503,
    )


def _try_recommend(engine: Any, task_type: str, source: str, policy: SourcePolicy) -> Any:
    """Call RoutingEngine.recommend, tolerating minor signature drift."""
    try:
        return engine.recommend(
            task_type=task_type,
            source=source,
            requires_tools=policy.requires_tools,
        )
    except TypeError:
        try:
            return engine.recommend(task_type=task_type, source=source)
        except Exception:
            return None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Request plumbing
# --------------------------------------------------------------------------- #
def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


def _source(request: Request) -> str:
    return request.headers.get("x-loom-source", "default")


def _extract_response_text(result: dict) -> Optional[str]:
    """Extract the assistant's text content from any response format."""
    for choice in result.get("choices", []):
        msg = choice.get("message", {})
        if msg.get("content"):
            return msg["content"]
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    return None


def _scan_response(gw: GatewayState, result: dict, provider: str, model: str, source: str) -> dict:
    """Scan LLM response text for sensitive data. Never raises."""
    if gw.scanner is None or not gw.scanner.enabled:
        return result
    try:
        choices = result.get("choices", [])
        modified = False
        for choice in choices:
            msg = choice.get("message", {})
            text = msg.get("content", "")
            if text:
                scanned, matches = gw.scanner.apply(
                    text, source=source, provider=provider, model=model,
                )
                if matches:
                    msg["content"] = scanned
                    modified = True
        if not modified:
            # Anthropic format: result.content[].text
            content = result.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            scanned, matches = gw.scanner.apply(
                                text, source=source, provider=provider, model=model,
                            )
                            if matches:
                                block["text"] = scanned
    except Exception:
        pass
    return result


def _error_response(exc: Exception, request_id: str, status: int = 500) -> JSONResponse:
    if isinstance(exc, ProviderError):
        payload = dict(exc.payload)
        payload.setdefault("error", {})
        if isinstance(payload.get("error"), dict):
            payload["error"].setdefault("request_id", request_id)
        return JSONResponse(payload, status_code=exc.status_code)
    import logging as _log
    _log.getLogger("loom.gateway").exception("Unhandled error (request_id=%s)", request_id)
    return JSONResponse(
        {
            "error": {
                "message": "internal server error",
                "type": "gateway_error",
                "request_id": request_id,
            }
        },
        status_code=status,
    )


async def _scan_ollama_stream(
    upstream: AsyncIterator[bytes],
    gw: GatewayState,
    request_id: str,
    source: str,
    provider: str,
    model: str,
    text_key: str,
) -> AsyncIterator[bytes]:
    """Buffer an Ollama NDJSON stream, scan assembled text, re-emit."""
    chunks: list[dict] = []
    raw_lines: list[bytes] = []
    async for line_bytes in upstream:
        raw_lines.append(line_bytes)
        try:
            chunk = json.loads(line_bytes)
            chunks.append(chunk)
        except (json.JSONDecodeError, ValueError):
            pass

    if text_key == "response":
        full_text = "".join(c.get("response", "") for c in chunks)
    else:
        full_text = "".join(
            c.get("message", {}).get("content", "") for c in chunks if isinstance(c.get("message"), dict)
        )

    if gw.scanner and full_text:
        scanned, _ = gw.scanner.apply(
            full_text, session_id=request_id,
            source=source, provider=provider, model=model,
        )
    else:
        scanned = full_text

    if scanned != full_text and chunks:
        first = chunks[0].copy()
        if text_key == "response":
            first["response"] = scanned
        else:
            first.setdefault("message", {})["content"] = scanned
        first["done"] = False
        yield (json.dumps(first) + "\n").encode("utf-8")
        final = chunks[-1].copy()
        if text_key == "response":
            final["response"] = ""
        else:
            final.setdefault("message", {})["content"] = ""
        final["done"] = True
        yield (json.dumps(final) + "\n").encode("utf-8")
    else:
        for line_bytes in raw_lines:
            yield line_bytes


async def _wrapped_stream(
    state: GatewayState,
    upstream: AsyncIterator[bytes],
    meta: dict,
    t0: float,
) -> AsyncIterator[bytes]:
    """Forward upstream bytes, scanning for sensitive data when buffering is enabled.

    If the scanner has buffer-mode rules, accumulates the full stream, scans the
    assembled text, rebuilds the SSE events with scanned content, and re-yields.
    Otherwise passes through directly with zero overhead.
    """
    status = 200
    scan_enabled = (
        state.scanner is not None
        and state.scanner.enabled
        and state.scanner.has_buffer_rules()
    )

    if scan_enabled:
        chunks: list[bytes] = []
        try:
            async for chunk in upstream:
                chunks.append(chunk)
        except ProviderError as exc:
            status = exc.status_code
            chunks.append(b"data: " + json.dumps(exc.payload).encode("utf-8") + b"\n\n")

        full_body = b"".join(chunks)
        source = meta.get("source", "unknown")
        provider = meta.get("provider", "unknown")
        model = meta.get("model", "")

        try:
            text_parts = []
            lines = full_body.decode("utf-8", errors="replace").split("\n")
            for line in lines:
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                    # OpenAI format
                    choices = event.get("choices", [])
                    for choice in choices:
                        delta = choice.get("delta", {})
                        if "content" in delta and delta["content"]:
                            text_parts.append(delta["content"])
                    # Anthropic format
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        text_parts.append(delta["text"])
                except json.JSONDecodeError:
                    pass

            if text_parts:
                full_text = "".join(text_parts)
                scanned_text, matches = state.scanner.apply(
                    full_text, source=source, provider=provider, model=model,
                )
                if matches:
                    # Redactions change text length, so we can't slice by
                    # original offsets. Instead, emit ALL scanned text in the
                    # first content-bearing delta and empty subsequent ones.
                    rebuilt_lines = []
                    emitted = False
                    for line in lines:
                        if not line.startswith("data: "):
                            rebuilt_lines.append(line)
                            continue
                        payload = line[6:].strip()
                        if not payload or payload == "[DONE]":
                            rebuilt_lines.append(line)
                            continue
                        try:
                            event = json.loads(payload)
                            changed = False
                            for choice in event.get("choices", []):
                                delta = choice.get("delta", {})
                                if "content" in delta and delta["content"]:
                                    delta["content"] = scanned_text if not emitted else ""
                                    emitted = True
                                    changed = True
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                delta["text"] = scanned_text if not emitted else ""
                                emitted = True
                                changed = True
                            if changed:
                                rebuilt_lines.append(f"data: {json.dumps(event)}")
                            else:
                                rebuilt_lines.append(line)
                        except json.JSONDecodeError:
                            rebuilt_lines.append(line)
                    full_body = "\n".join(rebuilt_lines).encode("utf-8")
        except Exception:
            pass  # scanning failure = pass through original

        yield full_body
    else:
        # Passthrough mode — zero overhead
        try:
            async for chunk in upstream:
                yield chunk
        except ProviderError as exc:
            status = exc.status_code
            body = json.dumps(exc.payload).encode("utf-8")
            yield b"data: " + body + b"\n\n"

    _record_request(
        state,
        status_code=status,
        latency_ms=round((time.monotonic() - t0) * 1000, 2),
        usage=None,
        cost=0.0,
        **meta,
    )


# --------------------------------------------------------------------------- #
#  Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    state: GatewayState = app.state.gateway
    state.config = LoomConfig.load()
    state.index_models()

    for provider in state.config.providers:
        state.backends[provider.name] = state.build_backend(
            provider.name, provider.api_base
        )

    if SensitiveDataScanner is not None:
        try:
            state.scanner = SensitiveDataScanner(state.config)
        except Exception:
            try:
                state.scanner = SensitiveDataScanner()  # type: ignore[call-arg]
            except Exception:
                state.scanner = None

    # Configure scanner Postgres modules when DSN is available
    pg_dsn = state.config.storage.postgres_dsn
    if pg_dsn:
        try:
            from loom.scanner import crypto, pseudonymizer

            crypto.configure(pg_dsn)
            pseudonymizer.configure(pg_dsn)
        except ImportError:
            pass

    try:
        state.storage = create_storage(state.config)
        state.storage.connect()
    except Exception:
        state.storage = None

    if ThrottleGovernor is not None:
        try:
            state.governor = ThrottleGovernor()
        except Exception:
            state.governor = None

    if AuditLogger is not None:
        try:
            state.audit = AuditLogger(
                audit_path=state.config.observability.audit_log_path,
                metrics_path=state.config.observability.metrics_log_path,
                scanner=state.scanner,
            )
        except Exception:
            state.audit = None

    if RoutingEngine is not None:
        try:
            state.routing = RoutingEngine(state.config)
        except Exception:
            try:
                state.routing = RoutingEngine()  # type: ignore[call-arg]
            except Exception:
                state.routing = None

    if DetectionEngine is not None:
        try:
            state.detection = DetectionEngine(state.config)
        except Exception:
            try:
                state.detection = DetectionEngine()  # type: ignore[call-arg]
            except Exception:
                state.detection = None

    if ContentProcessor is not None:
        try:
            state.compression = ContentProcessor(state.config)
        except Exception:
            try:
                state.compression = ContentProcessor()  # type: ignore[call-arg]
            except Exception:
                state.compression = None

    try:
        yield
    finally:
        for backend in state.backends.values():
            try:
                await backend.close()
            except Exception:
                pass
        if state.storage is not None:
            close = getattr(state.storage, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
#  App factory
# --------------------------------------------------------------------------- #
class _RateLimiter:
    """Simple in-memory per-IP rate limiter. No external dependencies."""

    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.setdefault(key, [])
            cutoff = now - self._window
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            return True


def create_app() -> FastAPI:
    app = FastAPI(title="Loom Gateway", version=__version__, lifespan=lifespan)
    app.state.gateway = GatewayState()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Loom-Request-Id"],
    )

    rate_limiter = _RateLimiter(max_requests=200, window_seconds=60)

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path = request.url.path
        _EXEMPT_PREFIXES = ("/health", "/api/models", "/api/metrics", "/api/audit", "/api/config", "/api/scanner", "/api/tags")
        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        if not rate_limiter.is_allowed(client_ip):
            return JSONResponse(
                {"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        gw = state()
        gw.request_count += 1
        response = await call_next(request)
        if response.status_code >= 500:
            gw.error_count += 1
        return response

    def state() -> GatewayState:
        return app.state.gateway

    # ----------------------------------------------------------- chat (OpenAI)
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        gw = state()
        request_id = str(uuid.uuid4())
        t0 = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return _error_response(
                ProviderError("invalid JSON body", status_code=400), request_id, 400
            )

        messages = body.get("messages") or []
        api_key = _bearer(request)
        source = _source(request)
        stream = bool(body.get("stream", False))

        try:
            model, task_type, routing_reason = _select_model(
                gw, body.get("model"), source, body, messages
            )
            resolved = gw.resolve_provider(model)
            if resolved is None:
                raise ProviderError(
                    f"unknown model '{model}' and provider could not be inferred",
                    status_code=400,
                )
            provider_name, model_cfg = resolved
            backend = gw.backends.get(provider_name)
            if backend is None:
                raise ProviderError(
                    f"no backend configured for provider '{provider_name}'",
                    status_code=500,
                )

            forward = _passthrough_params(body)

            # Session tracking.
            session_id = derive_session_id(messages, source)
            if gw.storage is not None and session_id != "unknown":
                try:
                    gw.storage.touch_session(session_id, source)
                except Exception:
                    pass

            # Strip thinking blocks from prior assistant turns.
            messages, thinking_stripped, thinking_bytes = _strip_thinking_blocks(messages)
            gw.thinking_blocks_stripped += thinking_stripped
            gw.thinking_bytes_saved += thinking_bytes

            # Inline compression: compress older messages before forwarding.
            comp_before = comp_after = 0
            if gw.compression is not None and len(messages) > 2:
                messages, comp_before, comp_after = _compress_messages_inline(
                    gw.compression, messages, gw.storage
                )
                gw.comp_tokens_before += comp_before
                gw.comp_tokens_after += comp_after

            result = await backend.chat_completion(
                model=model_cfg.model_id,
                messages=messages,
                api_key=api_key,
                stream=stream,
                **forward,
            )

            meta = {
                "request_id": request_id,
                "method": "POST",
                "path": "/v1/chat/completions",
                "source": source,
                "provider": provider_name,
                "model": model_cfg.model_id,
                "requested_model": body.get("model"),
                "task_type": task_type,
                "routing_reason": routing_reason,
                "compressed": comp_after < comp_before,
                "compression_ratio": (
                    round(comp_after / comp_before, 4) if comp_before > 0 else 1.0
                ),
            }

            if stream:
                return StreamingResponse(
                    _wrapped_stream(gw, result, meta, t0),  # type: ignore[arg-type]
                    media_type="text/event-stream",
                    headers={"X-Loom-Request-Id": request_id},
                )

            usage = _extract_usage(result, provider_name)
            normalized = _normalize_response(result, provider_name, model_cfg.model_id)
            resp_text = _extract_response_text(normalized)
            _record_request(
                gw,
                status_code=200,
                latency_ms=round((time.monotonic() - t0) * 1000, 2),
                usage=usage,
                cost=_model_cost(model_cfg, usage),
                messages=messages,
                response_text=resp_text,
                **meta,
            )
            normalized = _scan_response(gw, normalized, provider_name, model_cfg.model_id, source)
            return JSONResponse(normalized, headers={"X-Loom-Request-Id": request_id})
        except ProviderError as exc:
            _audit_error(gw, request_id, "/v1/chat/completions", source, exc.status_code)
            return _error_response(exc, request_id)
        except Exception as exc:  # never crash
            _audit_error(gw, request_id, "/v1/chat/completions", source, 500)
            return _error_response(exc, request_id)

    # ------------------------------------------- count_tokens (Anthropic passthrough)
    @app.post("/v1/messages/count_tokens")
    async def count_tokens_endpoint(request: Request):
        gw = state()
        request_id = str(uuid.uuid4())
        backend = gw.backends.get("anthropic")
        counter = getattr(backend, "count_tokens", None)
        if not callable(counter):
            return _error_response(
                ProviderError("no Anthropic backend configured", status_code=500),
                request_id,
                500,
            )
        try:
            body = await request.json()
        except Exception:
            return _error_response(
                ProviderError("invalid JSON body", status_code=400), request_id, 400
            )
        try:
            status_code, payload = await counter(body, dict(request.headers))
            return JSONResponse(
                payload,
                status_code=status_code,
                headers={"X-Loom-Request-Id": request_id},
            )
        except ProviderError as exc:
            return _error_response(exc, request_id)

    # -------------------------------------------------------- messages (Anthropic)
    @app.post("/v1/messages")
    async def messages_endpoint(request: Request):
        gw = state()
        request_id = str(uuid.uuid4())
        t0 = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return _error_response(
                ProviderError("invalid JSON body", status_code=400), request_id, 400
            )

        messages = body.get("messages") or []
        api_key = request.headers.get("x-api-key", "") or _bearer(request)
        source = _source(request)
        stream = bool(body.get("stream", False))

        try:
            model, task_type, routing_reason = _select_model(
                gw, body.get("model"), source, body, messages
            )
            # Force Anthropic backend for the messages API; fall back to inference.
            backend = gw.backends.get("anthropic")
            provider_name = "anthropic"
            model_cfg = None
            resolved = gw.resolve_provider(model)
            if resolved is not None:
                provider_name, model_cfg = resolved
                if backend is None:
                    backend = gw.backends.get(provider_name)
            if backend is None:
                raise ProviderError(
                    "no Anthropic backend configured", status_code=500
                )

            actual_model = model_cfg.model_id if model_cfg else model

            # Capture inbound headers & query string for upstream forwarding.
            client_headers = dict(request.headers)
            query_string = request.url.query or ""

            # Session tracking: stable conversation fingerprint, turn counter.
            session_id = derive_session_id(messages, source)
            if gw.storage is not None and session_id != "unknown":
                try:
                    gw.storage.touch_session(session_id, source)
                except Exception:
                    pass

            # Strip thinking blocks from prior assistant turns.
            messages, thinking_stripped, thinking_bytes = _strip_thinking_blocks(messages)
            gw.thinking_blocks_stripped += thinking_stripped
            gw.thinking_bytes_saved += thinking_bytes

            # Inline compression: compress older messages before forwarding.
            comp_before = comp_after = 0
            if gw.compression is not None and len(messages) > 2:
                messages, comp_before, comp_after = _compress_messages_inline(
                    gw.compression, messages, gw.storage
                )
                gw.comp_tokens_before += comp_before
                gw.comp_tokens_after += comp_after

            result = await backend.chat_completion(
                model=actual_model,
                messages=messages,
                api_key=api_key,
                stream=stream,
                inbound_headers=client_headers,
                query_string=query_string,
                raw_body=body,
            )

            meta = {
                "request_id": request_id,
                "method": "POST",
                "path": "/v1/messages",
                "source": source,
                "provider": provider_name,
                "model": actual_model,
                "requested_model": body.get("model"),
                "task_type": task_type,
                "routing_reason": routing_reason,
                "compressed": comp_after < comp_before,
                "compression_ratio": (
                    round(comp_after / comp_before, 4) if comp_before > 0 else 1.0
                ),
            }

            if stream:
                return StreamingResponse(
                    _wrapped_stream(gw, result, meta, t0),  # type: ignore[arg-type]
                    media_type="text/event-stream",
                    headers={"X-Loom-Request-Id": request_id},
                )

            usage = _extract_usage(result, provider_name)
            resp_text = _extract_response_text(result)
            _record_request(
                gw,
                status_code=200,
                latency_ms=round((time.monotonic() - t0) * 1000, 2),
                usage=usage,
                cost=_model_cost(model_cfg, usage),
                messages=messages,
                response_text=resp_text,
                **meta,
            )
            result = _scan_response(gw, result, provider_name, model, source)
            return JSONResponse(result, headers={"X-Loom-Request-Id": request_id})
        except ProviderError as exc:
            _audit_error(gw, request_id, "/v1/messages", source, exc.status_code)
            return _error_response(exc, request_id)
        except Exception as exc:
            _audit_error(gw, request_id, "/v1/messages", source, 500)
            return _error_response(exc, request_id)

    # ----------------------------------------------------------------- ollama-compat
    @app.post("/api/generate")
    async def ollama_generate(request: Request):
        gw = state()
        request_id = str(uuid.uuid4())
        source = _source(request)
        start = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return _error_response(ValueError("invalid JSON"), request_id, 400)

        model_name = body.get("model", "")
        prompt = body.get("prompt", "")
        stream = body.get("stream", False)

        if not model_name or not prompt:
            return JSONResponse(
                {"error": "model and prompt are required"},
                status_code=400,
                headers={"X-Loom-Request-Id": request_id},
            )

        # Programmatic search — skip LLM if search-shaped
        if get_search_tier is not None and gw.config.routing.programmatic_search_enabled:
            try:
                search = get_search_tier(gw.config.routing.search_sources).search(prompt)
                if search.tier == "zero-inference" and search.hits:
                    lines = [f"{h.file}:{h.line_number}: {h.line}" for h in search.hits]
                    result_text = f"Found {len(search.hits)} results:\n" + "\n".join(lines)
                    elapsed_ms = (time.monotonic() - start) * 1000
                    _record_request(
                        gw, request_id=request_id, method="POST", path="/api/generate",
                        source=source, provider="programmatic", model="zero-inference",
                        requested_model=model_name, task_type="search",
                        routing_reason=search.reason, status_code=200,
                        latency_ms=elapsed_ms,
                    )
                    from loom.gateway.reroute import _ollama_generate_envelope
                    return JSONResponse(
                        _ollama_generate_envelope(
                            model=model_name, response_text=result_text,
                            total_duration_ns=int(elapsed_ms * 1e6),
                        ),
                        headers={"X-Loom-Request-Id": request_id},
                    )
            except Exception:
                pass

        resolved = gw.resolve_provider(model_name)
        if resolved is None:
            return JSONResponse(
                {"error": f"unknown model: {model_name}"},
                status_code=400,
                headers={"X-Loom-Request-Id": request_id},
            )
        provider_name, model_cfg = resolved
        backend = gw.backends.get(provider_name)
        if backend is None:
            return JSONResponse(
                {"error": f"provider {provider_name} not configured"},
                status_code=400,
                headers={"X-Loom-Request-Id": request_id},
            )

        try:
            if hasattr(backend, "generate"):
                result = await backend.generate(
                    model=model_cfg.model_id, prompt=prompt, stream=stream,
                    **{k: body["options"][k] for k in body.get("options", {}) if k in ("temperature", "top_p", "top_k", "seed", "num_predict")},
                )
            else:
                messages = [{"role": "user", "content": prompt}]
                result = await backend.chat_completion(
                    model=model_cfg.model_id, messages=messages,
                    api_key=_bearer(request), stream=stream,
                )

            if stream:
                if gw.scanner and gw.scanner.enabled and gw.scanner.has_buffer_rules():
                    return StreamingResponse(
                        _scan_ollama_stream(result, gw, request_id, source, provider_name, model_name, "response"),
                        media_type="application/x-ndjson",
                    )
                return StreamingResponse(result, media_type="application/x-ndjson")

            # Scan response
            response_text = result.get("response", "")
            if gw.scanner and gw.scanner.enabled and response_text:
                scanned, _ = gw.scanner.apply(
                    response_text, session_id=request_id,
                    source=source, provider=provider_name, model=model_name,
                )
                if scanned != response_text:
                    result["response"] = scanned

            elapsed_ms = (time.monotonic() - start) * 1000
            usage = _extract_usage(result, provider_name)
            _record_request(
                gw, request_id=request_id, method="POST", path="/api/generate",
                source=source, provider=provider_name, model=model_cfg.model_id,
                requested_model=model_name, task_type="general",
                routing_reason="direct", status_code=200,
                latency_ms=elapsed_ms, usage=usage,
                cost=_model_cost(model_cfg, usage),
            )
            return JSONResponse(result, headers={"X-Loom-Request-Id": request_id})
        except ProviderError as exc:
            _audit_error(gw, request_id, "/api/generate", source, exc.status_code)
            return _error_response(exc, request_id)
        except Exception as exc:
            _audit_error(gw, request_id, "/api/generate", source, 500)
            return _error_response(exc, request_id)

    @app.post("/api/chat")
    async def ollama_chat(request: Request):
        gw = state()
        request_id = str(uuid.uuid4())
        source = _source(request)
        start = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return _error_response(ValueError("invalid JSON"), request_id, 400)

        model_name = body.get("model", "")
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        if not model_name or not messages:
            return JSONResponse(
                {"error": "model and messages are required"},
                status_code=400,
                headers={"X-Loom-Request-Id": request_id},
            )

        resolved = gw.resolve_provider(model_name)
        if resolved is None:
            return JSONResponse(
                {"error": f"unknown model: {model_name}"},
                status_code=400,
                headers={"X-Loom-Request-Id": request_id},
            )
        provider_name, model_cfg = resolved
        backend = gw.backends.get(provider_name)
        if backend is None:
            return JSONResponse(
                {"error": f"provider {provider_name} not configured"},
                status_code=400,
                headers={"X-Loom-Request-Id": request_id},
            )

        try:
            result = await backend.chat_completion(
                model=model_cfg.model_id, messages=messages,
                api_key=_bearer(request), stream=stream,
                **{k: body["options"][k] for k in body.get("options", {}) if k in ("temperature", "top_p", "top_k", "seed", "num_predict", "max_tokens")},
            )

            if stream:
                if gw.scanner and gw.scanner.enabled and gw.scanner.has_buffer_rules():
                    return StreamingResponse(
                        _scan_ollama_stream(result, gw, request_id, source, provider_name, model_name, "message.content"),
                        media_type="application/x-ndjson",
                    )
                return StreamingResponse(result, media_type="application/x-ndjson")

            # Scan response
            msg = result.get("message", {})
            response_text = msg.get("content", "") if isinstance(msg, dict) else ""
            if gw.scanner and gw.scanner.enabled and response_text:
                scanned, _ = gw.scanner.apply(
                    response_text, session_id=request_id,
                    source=source, provider=provider_name, model=model_name,
                )
                if scanned != response_text and isinstance(msg, dict):
                    result["message"]["content"] = scanned

            elapsed_ms = (time.monotonic() - start) * 1000
            usage = _extract_usage(result, provider_name)
            _record_request(
                gw, request_id=request_id, method="POST", path="/api/chat",
                source=source, provider=provider_name, model=model_cfg.model_id,
                requested_model=model_name, task_type="general",
                routing_reason="direct", status_code=200,
                latency_ms=elapsed_ms, usage=usage,
                cost=_model_cost(model_cfg, usage), messages=messages,
            )
            return JSONResponse(result, headers={"X-Loom-Request-Id": request_id})
        except ProviderError as exc:
            _audit_error(gw, request_id, "/api/chat", source, exc.status_code)
            return _error_response(exc, request_id)
        except Exception as exc:
            _audit_error(gw, request_id, "/api/chat", source, 500)
            return _error_response(exc, request_id)

    @app.get("/api/tags")
    async def ollama_tags(request: Request):
        gw = state()
        models = []
        for provider in gw.config.providers:
            for model in provider.models:
                models.append({
                    "name": model.display_name or model.model_id,
                    "model": model.model_id,
                    "size": 0,
                    "details": {
                        "provider": provider.name,
                        "tier": model.tier,
                        "supports_tools": model.supports_tools,
                    },
                })
        # Also fetch live Ollama models if available
        ollama_backend = gw.backends.get("ollama")
        if ollama_backend and hasattr(ollama_backend, "list_models_full"):
            try:
                live = await ollama_backend.list_models_full()
                seen = {m["model"] for m in models}
                for m in live.get("models", []):
                    if m.get("name") not in seen and m.get("model") not in seen:
                        models.append(m)
            except Exception:
                pass
        return JSONResponse({"models": models})

    @app.post("/api/show")
    async def ollama_show(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        name = body.get("name", "")
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        gw = state()
        ollama_backend = gw.backends.get("ollama")
        if ollama_backend and hasattr(ollama_backend, "show_model"):
            try:
                result = await ollama_backend.show_model(name)
                if result:
                    return JSONResponse(result)
            except Exception:
                pass
        resolved = gw.resolve_provider(name)
        if resolved:
            _, model_cfg = resolved
            return JSONResponse({
                "modelfile": "",
                "parameters": "",
                "template": "",
                "details": {
                    "model_id": model_cfg.model_id,
                    "tier": model_cfg.tier,
                    "max_context_tokens": model_cfg.max_context_tokens,
                },
            })
        return JSONResponse({"error": f"model not found: {name}"}, status_code=404)

    # ----------------------------------------------------------------- compress
    @app.post("/v1/compress")
    async def compress(request: Request):
        gw = state()
        request_id = str(uuid.uuid4())
        try:
            body = await request.json()
        except Exception:
            return _error_response(
                ProviderError("invalid JSON body", status_code=400), request_id, 400
            )

        messages = body.get("messages") or []
        mode = body.get("mode", "audit")
        if gw.compression is None:
            return JSONResponse(
                {
                    "messages": messages,
                    "stats": {"enabled": False, "reason": "compression unavailable"},
                    "request_id": request_id,
                }
            )

        compressed: list[dict] = []
        original_chars = 0
        compressed_chars = 0
        n = len(messages)
        try:
            for idx, msg in enumerate(messages):
                content = msg.get("content", "")
                text = content if isinstance(content, str) else json.dumps(content)
                original_chars += len(text)
                age_ratio = idx / max(n - 1, 1) if n > 1 else 0.0
                if mode == "audit":
                    new_text = text
                else:
                    new_text = _run_compress_graduated(gw.compression, text, age_ratio)
                compressed_chars += len(new_text)
                out = dict(msg)
                out["content"] = new_text
                compressed.append(out)
        except Exception as exc:
            return _error_response(exc, request_id)

        ratio = (compressed_chars / original_chars) if original_chars else 1.0
        return JSONResponse(
            {
                "messages": compressed,
                "stats": {
                    "mode": mode,
                    "original_chars": original_chars,
                    "compressed_chars": compressed_chars,
                    "compression_ratio": round(ratio, 4),
                    "chars_saved": original_chars - compressed_chars,
                },
                "request_id": request_id,
            }
        )

    # ------------------------------------------------------------------- detect
    @app.post("/v1/detect")
    async def detect(request: Request):
        gw = state()
        request_id = str(uuid.uuid4())
        try:
            body = await request.json()
        except Exception:
            return _error_response(
                ProviderError("invalid JSON body", status_code=400), request_id, 400
            )

        source = body.get("source", "default")
        prompt = body.get("prompt", "")
        if gw.detection is None:
            return JSONResponse(
                {
                    "tier": None,
                    "detail": "detection engine unavailable",
                    "request_id": request_id,
                }
            )
        try:
            result = _run_detect(gw.detection, source, prompt)
        except Exception as exc:
            return _error_response(exc, request_id)
        return JSONResponse({"request_id": request_id, **_jsonable(result)})

    # ------------------------------------------------------------------- health
    @app.get("/health")
    async def health():
        gw = state()
        return {
            "status": "healthy",
            "version": __version__,
            "uptime_seconds": round(time.time() - gw.started_at, 1),
            "requests": gw.request_count,
            "errors": gw.error_count,
            "providers": [p.name for p in gw.config.providers],
            "routing_table_loaded": gw.routing is not None,
            "detection_enabled": gw.detection is not None,
            "scanner_enabled": gw.scanner is not None and gw.scanner.enabled,
            # Observability contract blocks (docs/observability-api.md).
            # Compression rollup covers this process lifetime (estimated
            # tokens over compression-eligible messages).
            "compression": {
                "enabled": gw.compression is not None,
                "default_tier": gw.config.compression.default_tier,
                "tokens_before": gw.comp_tokens_before,
                "tokens_after": gw.comp_tokens_after,
                "tokens_saved": gw.comp_tokens_before - gw.comp_tokens_after,
                "compression_ratio": (
                    round(1.0 - gw.comp_tokens_after / gw.comp_tokens_before, 3)
                    if gw.comp_tokens_before > 0
                    else 0.0
                ),
            },
            "thinking_strip": {
                "blocks_stripped": gw.thinking_blocks_stripped,
                "bytes_saved": gw.thinking_bytes_saved,
            },
            "sessions": _session_stats_block(gw),
        }

    # ------------------------------------------------------- costs (contract)
    def _input_rate_per_1k(gw: GatewayState, model: str) -> float:
        entry = gw.model_index.get(model)
        return entry[1].cost_per_1k_input if entry else 0.0

    @app.get("/api/costs")
    async def api_costs(days: int = 30):
        gw = state()
        empty = {
            "window_days": days,
            "totals": {
                "requests": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "tokens_saved": 0,
                "savings_usd": 0.0,
            },
            "by_model": [],
            "by_source": [],
            "by_tier": [],
            "by_day": [],
            "by_hour": [],
        }
        if gw.storage is None:
            return empty
        try:
            summary = _jsonable(gw.storage.get_cost_summary(days))
        except Exception:
            return empty

        total_savings = 0.0
        for bucket in summary["by_model"]:
            rate = _input_rate_per_1k(gw, bucket["model"])
            bucket["savings_usd"] = round(bucket["tokens_saved"] / 1000 * rate, 4)
            total_savings += bucket["savings_usd"]
        for bucket in summary["by_source"] + summary["by_day"]:
            bucket.setdefault("savings_usd", 0.0)
        for bucket in summary["by_hour"]:
            bucket.setdefault("savings_usd", 0.0)
        summary["totals"]["savings_usd"] = round(total_savings, 4)
        return summary

    # ---------------------------------------------------- sessions (contract)
    def _session_stats_block(gw: GatewayState) -> dict:
        if gw.storage is None:
            return {"supported": False, "sessions": 0, "total_turns": 0}
        try:
            stats = gw.storage.get_session_stats()
            return {"supported": True, **stats}
        except Exception:
            return {"supported": False, "sessions": 0, "total_turns": 0}

    @app.get("/api/sessions")
    async def api_sessions(hours: int = 24):
        gw = state()
        block = _session_stats_block(gw)
        entries: list[dict] = []
        if block["supported"]:
            try:
                entries = _jsonable(gw.storage.list_sessions(hours=hours))
            except Exception:
                pass
        return {**block, "hours": hours, "entries": entries}

    # -------------------------------------------------------------- routing log
    @app.get("/api/routing")
    async def api_routing(hours: int = 24, limit: int = 200):
        gw = state()
        if gw.storage is None:
            return {"available": False, "hours": hours, "total": 0, "entries": [], "by_reason": {}, "overrides": 0}
        try:
            return {"available": True, **_jsonable(gw.storage.get_routing_decisions(hours=hours, limit=limit))}
        except Exception:
            return {"available": False, "hours": hours, "total": 0, "entries": [], "by_reason": {}, "overrides": 0}

    # ------------------------------------------------------------------- models
    @app.get("/api/models")
    async def api_models():
        gw = state()
        models = []
        for provider in gw.config.providers:
            for model in provider.models:
                models.append(
                    {
                        "id": model.model_id,
                        "display_name": model.display_name,
                        "provider": provider.name,
                        "tier": model.tier,
                        "supports_tools": model.supports_tools,
                        "supports_json_mode": model.supports_json_mode,
                        "max_context_tokens": model.max_context_tokens,
                    }
                )
        return {"object": "list", "data": models}

    # ------------------------------------------------------------------ metrics
    @app.get("/api/metrics")
    async def api_metrics():
        gw = state()
        if gw.storage is None:
            return {"available": False, "metrics": {}}
        try:
            return {"available": True, "metrics": _jsonable(gw.storage.get_routing_stats(24))}
        except Exception:
            return {"available": False, "metrics": {}}

    # -------------------------------------------------------- metrics timeseries
    @app.get("/api/metrics/timeseries")
    async def api_metrics_timeseries(hours: int = 24, bucket: str = "1h"):
        gw = state()
        empty = {
            "hours": hours,
            "bucket_seconds": _bucket_seconds(bucket),
            "buckets": [],
            "by_model": {},
            "by_source": {},
            "by_task_type": {},
        }
        if gw.storage is None:
            return empty
        try:
            return _jsonable(
                gw.storage.get_metrics_timeseries(hours, _bucket_seconds(bucket))
            )
        except Exception:
            return empty

    # ------------------------------------------------------------------- audit
    @app.get("/api/audit")
    async def api_audit(
        limit: int = 50,
        offset: int = 0,
        model: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ):
        gw = state()
        if gw.storage is None:
            return {"total": 0, "offset": offset, "limit": limit, "entries": []}
        try:
            page = _jsonable(
                gw.storage.get_audit_entries(
                    limit=limit,
                    offset=offset,
                    model=model,
                    source=source,
                    status=status,
                    search=search,
                )
            )
            # Contract aliases (docs/observability-api.md) alongside the
            # storage-native names so both consumer generations work.
            for entry in page.get("entries", []):
                entry.setdefault("model", entry.get("model_used"))
                entry.setdefault("cost_usd", entry.get("cost_estimate"))
            return page
        except Exception:
            return {"total": 0, "offset": offset, "limit": limit, "entries": []}

    # ------------------------------------------------------------------- config
    @app.get("/api/config")
    async def api_config():
        gw = state()
        return _sanitized_config(gw.config)

    # -------------------------------------------------------- scanner management
    @app.get("/api/scanner/rules")
    async def api_scanner_rules():
        gw = state()
        if gw.scanner is None:
            return {"enabled": False, "rules": [], "skip_config": {}}
        return {
            "enabled": gw.scanner.enabled,
            "rules": gw.scanner.rules_summary(),
            "skip_config": gw.scanner.skip_config(),
        }

    @app.put("/api/scanner/rules/{name}")
    async def api_scanner_update_rule(name: str, request: Request):
        gw = state()
        if gw.scanner is None:
            return JSONResponse({"error": "scanner not available"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        allowed = {"enabled", "action", "mask_format", "streaming_mode"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return JSONResponse({"error": "no valid fields"}, status_code=400)
        if gw.scanner.update_rule(name, updates):
            return {"status": "updated", "rule": name, "updates": updates}
        return JSONResponse({"error": f"rule '{name}' not found"}, status_code=404)

    @app.get("/api/scanner/stats")
    async def api_scanner_stats():
        gw = state()
        if gw.scanner is None:
            return {"enabled": False, "total_scans": 0, "total_detections": 0, "by_rule": {}}
        return {"enabled": gw.scanner.enabled, **gw.scanner.stats()}

    # ----------------------------------------------------------- governor
    @app.get("/api/governor/status")
    async def api_governor_status():
        gw = state()
        if gw.governor is None:
            return JSONResponse({"error": "governor not available"}, status_code=503)
        return gw.governor.status()

    @app.get("/api/governor")
    async def api_governor_get():
        gw = state()
        if gw.governor is None:
            return JSONResponse({"error": "governor not available"}, status_code=503)
        return gw.governor.get_settings()

    @app.patch("/api/governor")
    async def api_governor_update(request: Request):
        gw = state()
        if gw.governor is None:
            return JSONResponse({"error": "governor not available"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        allowed = {"enabled", "tier_thresholds", "class_overrides"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return JSONResponse({"error": "no valid fields"}, status_code=400)
        try:
            return gw.governor.update(updates, actor="dashboard")
        except GovernorValidationError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.delete("/api/governor/class-overrides/{job}")
    async def api_governor_delete_override(job: str):
        gw = state()
        if gw.governor is None:
            return JSONResponse({"error": "governor not available"}, status_code=503)
        return gw.governor.delete_class_override(job, actor="dashboard")

    # ----------------------------------------------------------- dashboard (SPA)
    # Mounted LAST so API routes always take priority over the static catch-all.
    dashboard_dir = (
        pathlib.Path(__file__).parent.parent / "dashboard" / "static"
    )
    if dashboard_dir.exists():
        app.mount(
            "/",
            StaticFiles(directory=str(dashboard_dir), html=True),
            name="dashboard",
        )

    return app


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #
_OPENAI_PASSTHROUGH = (
    "temperature", "top_p", "n", "stop", "max_tokens", "max_completion_tokens",
    "presence_penalty", "frequency_penalty", "logit_bias", "user", "seed",
    "response_format", "tools", "tool_choice", "functions", "function_call",
    "parallel_tool_calls", "logprobs", "top_logprobs",
)
_ANTHROPIC_PASSTHROUGH = (
    "temperature", "top_p", "top_k", "max_tokens", "stop_sequences", "system",
    "tools", "tool_choice", "metadata", "thinking",
)


def _passthrough_params(body: dict, anthropic: bool = False) -> dict:
    keys = _ANTHROPIC_PASSTHROUGH if anthropic else _OPENAI_PASSTHROUGH
    return {k: body[k] for k in keys if k in body and body[k] is not None}


_BUCKET_SIZES = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def _bucket_seconds(bucket: str) -> int:
    return _BUCKET_SIZES.get((bucket or "1h").lower(), 3600)


# Tag appended to compressed messages so the gateway skips re-compression on
# subsequent turns of the same conversation: <!--loom:compressed:TIER:HASH-->
_LOOM_TAG_RE = re.compile(r"<!--loom:compressed:(\w+):([a-f0-9]{8,16})-->\s*$")


def _strip_loom_tag(text: str) -> tuple[str, Optional[str]]:
    """Return (text, tier) — tier is None when the text carries no tag."""
    m = _LOOM_TAG_RE.search(text)
    if m:
        return text, m.group(1)
    return text, None


def derive_session_id(messages: list[dict], source: str) -> str:
    """Stable conversation fingerprint: source + first user message prefix.

    Same derivation as the legacy proxy so session identities survive the
    cutover within a running conversation.
    """
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    if first_user is None:
        return "unknown"
    content = first_user.get("content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                content = block.get("text", "")
                break
        else:
            content = json.dumps(content)
    seed = f"{source}:{str(content)[:256]}"
    return "gw-" + hashlib.sha256(seed.encode()).hexdigest()[:16]



def _strip_thinking_blocks(
    messages: list[dict],
) -> tuple[list[dict], int, int]:
    """Remove thinking blocks from prior assistant turns.

    Walks the *messages* array and strips content blocks with
    type: "thinking" from every assistant message **except** the last
    one (which may still be relevant for the current turn).

    Returns (*messages*, *blocks_stripped*, *bytes_saved*).
    """
    # Find the index of the last assistant message.
    last_assistant_idx: int = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break

    blocks_stripped = 0
    bytes_saved = 0

    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant" or idx == last_assistant_idx:
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        filtered: list[dict] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                blocks_stripped += 1
                # Estimate bytes from the thinking text.
                thinking_text = block.get("thinking", "")
                bytes_saved += len(thinking_text.encode("utf-8", errors="replace"))
            else:
                filtered.append(block)
        if len(filtered) != len(content):
            # If all blocks were thinking blocks, keep a minimal text block
            # so the message content array is never empty.
            if not filtered:
                filtered = [{"type": "text", "text": ""}]
            msg["content"] = filtered

    return messages, blocks_stripped, bytes_saved


def _estimate_tokens_safe(text: str) -> int:
    try:
        from loom.compression.processor import _estimate_tokens

        return _estimate_tokens(text)
    except Exception:
        return max(1, len(text) // 4)


def _compress_messages_inline(
    processor: Any,
    messages: list[dict],
    storage: Any = None,
) -> tuple[list[dict], int, int]:
    """Compress older messages before forwarding to the provider.

    Skips the last 2 messages (active context) and applies graduated
    compression to everything else — oldest messages get compressed most.
    Messages already carrying a loom:compressed tag are passed through
    untouched (double-compression prevention); the storage compression cache
    is consulted before compressing and updated after.

    Returns (messages, tokens_before, tokens_after) — estimated tokens over
    the compression-eligible messages only.
    """
    n = len(messages)
    if n <= 2:
        return messages, 0, 0
    compressed: list[dict] = []
    tokens_before = 0
    tokens_after = 0
    for idx, msg in enumerate(messages):
        if idx >= n - 2:
            compressed.append(msg)
            continue
        content = msg.get("content", "")
        text = content if isinstance(content, str) else json.dumps(content)

        _, existing_tier = _strip_loom_tag(text)
        if existing_tier is not None:
            # Compressed on a previous turn — count as already-saved via the
            # tag, don't recompress (originals are gone).
            tb = _estimate_tokens_safe(text)
            tokens_before += tb
            tokens_after += tb
            compressed.append(msg)
            continue

        age_ratio = idx / max(n - 1, 1)
        tb = _estimate_tokens_safe(text)
        tokens_before += tb

        new_text = None
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        if storage is not None:
            try:
                hit = storage.get_compression_cached(content_hash, age_ratio)
                if hit:
                    new_text = hit["compressed_text"]
            except Exception:
                pass

        if new_text is None:
            new_text, tier = _run_compress_graduated(processor, text, age_ratio)
            if new_text != text and len(new_text) < len(text):
                new_text = f"{new_text}\n<!--loom:compressed:{tier}:{content_hash}-->"
                if storage is not None:
                    try:
                        storage.put_compression_cached(
                            content_hash=content_hash,
                            age_ratio=age_ratio,
                            compressed=new_text,
                            tier=tier,
                            tokens_before=tb,
                            tokens_after=_estimate_tokens_safe(new_text),
                        )
                    except Exception:
                        pass
            else:
                new_text = text

        ta = _estimate_tokens_safe(new_text)
        tokens_after += ta
        if new_text != text:
            out = dict(msg)
            out["content"] = new_text
            compressed.append(out)
        else:
            compressed.append(msg)
    return compressed, tokens_before, tokens_after


def _run_compress_graduated(
    processor: Any, text: str, age_ratio: float
) -> tuple[str, str]:
    """Returns (compressed_text, tier_name)."""
    fn = getattr(processor, "compress_graduated", None)
    if callable(fn):
        try:
            result = fn(text, age_ratio)
            if isinstance(result, tuple):
                return result[0], str(result[1])
            if isinstance(result, str):
                return result, "medium"
        except Exception:
            pass
    return text, "full"


def _run_detect(engine: Any, source: str, prompt: str) -> Any:
    fn = getattr(engine, "detect", None)
    if not callable(fn):
        return {"tier": None}
    try:
        return fn(source=source, prompt=prompt)
    except TypeError:
        try:
            return fn(source, prompt)
        except TypeError:
            return fn(prompt)


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    for method in ("to_dict", "model_dump", "dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                return _jsonable(fn())
            except Exception:
                pass
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _sanitized_config(config: LoomConfig) -> dict:
    return {
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "log_level": config.server.log_level,
            "display_timezone": config.server.display_timezone,
        },
        "providers": [
            {
                "name": p.name,
                "api_base": p.api_base,
                "models": [m.model_id for m in p.models],
            }
            for p in config.providers
        ],
        "sources": {
            name: {
                "minimum_tier": s.minimum_tier,
                "requires_tools": s.requires_tools,
                "allowed_providers": s.allowed_providers,
                "budget_tier": s.budget_tier,
                "pinned_model": s.pinned_model,
            }
            for name, s in config.sources.items()
        },
        "routing": {
            "default_determinism_target": config.routing.default_determinism_target,
            "min_empirical_runs": config.routing.min_empirical_runs,
        },
        "compression": {"enabled": config.compression.enabled},
    }


# Module-level ASGI app for ``uvicorn loom.gateway.app:app``.
app = create_app()
