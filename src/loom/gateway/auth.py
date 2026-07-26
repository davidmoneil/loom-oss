"""Gateway authentication policy.

Default-protected: every route requires a gateway key once at least one key
exists, except a small public allowlist (health, model listings, docs, and
the dashboard shell). Before any key is created the gateway runs open —
that fail-open state is deliberate for first-run setup, and the gateway
logs it loudly and reports it via /health so it can't be missed.

OAuth passthrough (``server.oauth_passthrough``) only applies to inference
routes (``/v1/*``): an Anthropic OAuth bearer is forwarded upstream where
Anthropic validates it. It never grants access to the admin/observability
API.
"""

from __future__ import annotations

from typing import Optional

# Only /api/* and /v1/* carry data or mutate state; everything else is the
# dashboard shell, static assets, docs, or the SPA's client-side routes
# (whose deep links are served index.html by the 404 fallback) — all public.
# Within the API surface, these exact paths stay open: model listings are
# needed by clients for discovery before they authenticate, and /health is
# the monitoring contract.
_PROTECTED_PREFIXES = ("/api/", "/v1/")

_PUBLIC_API_PATHS = frozenset({"/v1/models", "/api/models", "/api/tags"})

# OAuth passthrough may satisfy auth ONLY for these prefixes (inference).
_OAUTH_ELIGIBLE_PREFIXES = ("/v1/",)


def is_public_path(path: str) -> bool:
    if not path.startswith(_PROTECTED_PREFIXES):
        return True
    return path in _PUBLIC_API_PATHS


def gateway_keys_exist(gw) -> bool:
    """Cached 'any key rows exist' check (invalidated on create/delete)."""
    cached = getattr(gw, "_gateway_keys_exist", None)
    if cached is None:
        cached = bool(gw.storage.list_gateway_keys()) if gw.storage else False
        gw._gateway_keys_exist = cached
    return cached


def check_request_auth(
    path: str,
    raw_key: str,
    bearer: str,
    gw,
    oauth_passthrough_enabled: bool,
) -> Optional[str]:
    """Return None when the request may proceed, else a denial reason.

    ``raw_key`` is the extracted gateway credential; ``bearer`` is the raw
    Authorization bearer (used only for the OAuth passthrough check).
    """
    if is_public_path(path):
        return None
    if gw.storage is None or not gateway_keys_exist(gw):
        return None  # fail-open: no keys provisioned yet (loudly reported)
    if (
        oauth_passthrough_enabled
        and bearer.startswith("sk-ant-oat")
        and path.startswith(_OAUTH_ELIGIBLE_PREFIXES)
    ):
        return None
    if raw_key and gw.storage.validate_gateway_key(raw_key):
        return None
    return "present" if raw_key else "missing"
