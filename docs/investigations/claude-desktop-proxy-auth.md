# Claude Desktop + Loom Proxy: Authentication Investigation

**Date**: 2026-07-26
**Status**: Resolved via Option B + C (2026-07-26) — Loom's `server.oauth_passthrough` flag lets `sk-ant-oat*` bearer tokens bypass gateway key auth; Claude Desktop is configured with base URL only (no credential) so the claude.ai OAuth login stays active. Pending live verification with Claude Desktop.

## Problem Statement

Route Claude Desktop (MAX subscription) traffic through Loom for compression and observability, while maintaining MAX billing/limits.

## What We Built (PR #67, merged)

1. **`x-loom-gateway-key` header** — Separates gateway authentication from the provider API key. Loom checks this header first for gateway auth, keeping `Authorization` and `x-api-key` free for the upstream provider.

2. **`x-loom-client` header** — Explicit client-type identification in session metadata (e.g., `claude-desktop`), takes priority over User-Agent heuristics.

3. **`x-loom-*` header stripping** — All `x-loom-*` headers are stripped before forwarding to upstream providers. Prevents leaking gateway-internal headers.

4. **`_provider_api_key()` function** — Intelligently extracts the upstream provider API key by skipping whichever header carried the gateway key. If `x-api-key` has the gateway key, it returns `Authorization: Bearer`. If `x-loom-gateway-key` has it, both `x-api-key` and `Authorization` are available.

5. **`_gateway_key()` function** — Checks `x-loom-gateway-key`, then `x-api-key`, then `Authorization: Bearer` for gateway authentication. Backward compatible with Claude Code (which uses `Authorization: Bearer`).

6. **`docs/claude-desktop-setup.md`** — Setup guide (now partially outdated given findings below).

## The Core Finding

**Claude Desktop does NOT send Anthropic credentials through the third-party inference proxy when a gateway credential is configured.**

### Evidence (full header dump from production logs)

When Claude Desktop connects to Loom via third-party inference with a gateway credential configured, it sends ONLY:

| Header | Value | Purpose |
|--------|-------|---------|
| `x-api-key` | `loom-Y5XqMUl...` | The credential from the 3P config form (our gateway key) |
| `x-loom-gateway-key` | `loom-Y5XqMUl...` | Custom header we configured (same gateway key) |
| `x-loom-client` | `claude-desktop` | Custom header we configured |
| `anthropic-version` | `2023-06-01` | Anthropic API version |
| `user-agent` | `Claude/1.24012.9 Chrome/148...` | Electron app UA |
| `content-type` | `application/json` | Standard |
| Various | `sec-fetch-*`, `sentry-trace`, `baggage`, etc. | Browser/Electron internals |

**Missing**: No `authorization` header. No OAuth token. No cookie. No separate Anthropic API key. We verified this by logging ALL inbound headers — Loom is not stripping anything.

### What this means

Per Claude Desktop's own documentation (relayed by the user):

> "Setting only the base URL, without a gateway credential, doesn't replace the subscription — requests still route through the gateway, but the saved claude.ai login stays the active credential."

> "Setting a gateway credential takes you off your subscription."

When a gateway credential IS set (as we must, since Loom has key auth enabled), Claude Desktop treats the third-party gateway as a standalone API provider, NOT as a proxy for Anthropic. It sends the gateway credential in `x-api-key` and expects the gateway itself to handle upstream authentication.

## Error Flow

1. Claude Desktop sends `x-api-key: loom-Y5XqMUl...` (gateway key only)
2. Loom gateway auth validates it ✅ (gateway key is correct)
3. Loom extracts `api_key` for the upstream provider — finds no separate Anthropic credential
4. Request forwarded to `api.anthropic.com` with no auth headers
5. Anthropic returns `401: invalid x-api-key`

## What Works Today

- **Claude Code through Loom** ✅ — Claude Code sends `ANTHROPIC_API_KEY` as its API key via `Authorization: Bearer`. Loom validates the gateway key (also from `Authorization: Bearer` when `x-loom-gateway-key` isn't set, or separately when it is). The Anthropic key passes through to the upstream provider.

- **Gateway key auth** ✅ — The `x-loom-gateway-key` / `x-api-key` / `Authorization: Bearer` fallback chain works correctly.

- **Header stripping** ✅ — Verified that `x-loom-*` headers are stripped before forwarding upstream.

- **Model discovery** ✅ — Claude Desktop finds all 6 configured models via `GET /v1/models`.

## Options to Resolve

### Option A: Configure a default provider API key in Loom

Add a `default_api_key` field to the Anthropic provider config. When a request arrives with no provider API key (only a gateway key), Loom injects this key before forwarding upstream.

- **Pro**: No changes needed on Claude Desktop side
- **Pro**: Gateway key still authenticates the client to Loom
- **Con**: Requires an Anthropic API key (available via console.anthropic.com; MAX subscription includes API access at no extra cost)
- **Con**: All Claude Desktop requests bill against the API key's account, not the MAX subscription

### Option B: Credential-less third-party inference (if possible)

Configure Claude Desktop with only the base URL, no gateway credential. Per the docs, this keeps MAX subscription auth active.

- **Pro**: Keeps MAX billing
- **Pro**: Claude Desktop sends its own OAuth token
- **Con**: Loom's gateway key auth would need to be bypassed or disabled for these requests
- **Con**: Unknown if Claude Desktop's 3P form even allows credential-less config — "it prompts for a credential kind, which suggests not"

### Option C: OAuth token passthrough

If Option B works and Claude Desktop sends an OAuth token via `Authorization: Bearer`, Loom could detect it (OAuth tokens start with `sk-ant-oat`) and skip gateway key validation for OAuth-authenticated requests, forwarding the token directly to Anthropic.

- **Pro**: Keeps MAX billing, keeps Loom observability
- **Con**: Depends on Option B being possible
- **Con**: Bypasses Loom's gateway auth for these requests (security tradeoff)

## Current State of the Codebase

The following changes are on `main` but not yet committed as a single clean PR (deployed directly for debugging):

- `_gateway_key()` checks `x-loom-gateway-key` → `x-api-key` → `Authorization: Bearer`
- `_provider_api_key()` skips the gateway key header and returns the other auth header
- Debug logging of all inbound headers (should be removed before final commit)
- `_STRIP_HEADERS` includes `x-loom-gateway-key` and `x-loom-client`
- `x-loom-*` prefix catch-all in header forwarding

## Files Changed

- `src/loom/gateway/app.py` — `_gateway_key()`, `_provider_api_key()`, gateway auth middleware, session signal extraction, debug logging
- `src/loom/gateway/providers/anthropic.py` — `_STRIP_HEADERS` additions, `x-loom-*` prefix filter
- `tests/test_gateway.py` — Tests for gateway key priority, fallback, and `x-loom-client` detection
- `docs/claude-desktop-setup.md` — Setup guide (needs update based on findings)
