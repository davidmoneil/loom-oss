# Using Loom with Claude Desktop

Route Claude Desktop's API traffic through Loom for compression and observability.

## How It Works

Claude Desktop lets you configure a custom base URL for Anthropic API calls (the
third-party inference form, or `anthropicBaseUrl` in the developer config). Point it at
Loom, and all chat traffic flows through the gateway.

**Important credential behavior**: if you enter *any* credential in Claude Desktop's
third-party inference form, the app abandons your claude.ai (MAX subscription) OAuth
login and sends only that credential. There is no way to send both a gateway key and
your OAuth token from that form. This leads to two distinct setups:

- **OAuth passthrough (recommended for subscription users)** — configure the base URL
  *only*, leave the credential blank, and enable `oauth_passthrough` in Loom. Claude
  Desktop keeps using your claude.ai login; Loom lets the OAuth token through and
  Anthropic validates it upstream. Usage bills to your subscription, not an API key.
- **API key + gateway key** — enter your Anthropic API key as the credential (billing
  moves to that key), or use the developer-config `defaultHeaders` route with a
  `x-loom-gateway-key` header if your Claude Desktop build supports it.

## Setup A: OAuth Passthrough (subscription billing)

### 1. Enable passthrough in Loom

In `loom.yaml`:

```yaml
server:
  oauth_passthrough: true
```

Or via environment: `LOOM_OAUTH_PASSTHROUGH=1`. Restart Loom.

With passthrough enabled, requests carrying an Anthropic OAuth token
(`Authorization: Bearer sk-ant-oat...`) skip gateway key validation. Everything else
still requires a gateway key when key auth is enabled. The token is never stored by
Loom; Anthropic validates it upstream, and the per-IP rate limiter still applies.

### 2. Configure Claude Desktop

In the third-party inference / custom base URL setting:

- **Base URL**: `http://localhost:4444` (or your Loom host)
- **Credential**: leave **blank** — entering one disables your claude.ai login

Restart Claude Desktop and confirm you are still signed in to your claude.ai account.

## Setup B: API Key + Gateway Key

Use this when you want Claude Desktop traffic billed to an Anthropic API key, or when
you cannot enable `oauth_passthrough`.

### 1. Create a gateway key (if key auth is enabled)

From the Loom dashboard (Settings → Gateway Keys) or via the API:

```bash
curl -s http://localhost:4444/api/gateway-keys \
  -H 'Content-Type: application/json' \
  -d '{"name": "claude-desktop"}' | jq .
```

Save the returned key — it is shown only once.

### 2. Configure Claude Desktop

If your build exposes Developer → Edit Config with header support:

```json
{
  "anthropicBaseUrl": "http://localhost:4444",
  "defaultHeaders": {
    "x-loom-gateway-key": "gk-your-key-here",
    "x-loom-client": "claude-desktop",
    "x-loom-source": "desktop"
  }
}
```

**Header reference:**

| Header | Purpose | Required |
|--------|---------|----------|
| `x-loom-gateway-key` | Gateway authentication (separate from your Anthropic API key) | Only if key auth is enabled |
| `x-loom-client` | Identifies this client in session metadata and the dashboard | Recommended |
| `x-loom-source` | Routes requests through the `desktop` source policy in `loom.yaml` | Optional |

With `x-loom-gateway-key` present, the middleware validates it as the gateway key and
the `Authorization` header passes through untouched to the upstream provider. The
`x-loom-gateway-key` header is stripped before forwarding.

## (Optional) Source policy

In `loom.yaml`, add a source-specific configuration:

```yaml
sources:
  desktop:
    tier: light           # compression tier
    model_preference:
      - claude-sonnet-4-20250514
```

Without a dedicated source policy, requests from Claude Desktop use the `default` source.

## Verify

```bash
# Check Loom is running
curl -sf http://localhost:4444/health | jq .requests

# Send a message in Claude Desktop

# Check the request count increased
curl -sf http://localhost:4444/health | jq .requests

# Check the dashboard — the request should show client_type "claude-desktop"
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| 401 from Anthropic after adding a credential | Entering a credential in the third-party form disabled the claude.ai OAuth login | Clear the credential (base URL only) and enable `oauth_passthrough` in Loom |
| 401 `invalid gateway key` from Loom | Key auth is on and the request has neither a valid gateway key nor an OAuth token with passthrough enabled | Enable `oauth_passthrough` (Setup A) or supply `x-loom-gateway-key` (Setup B) |
| Connection refused | Loom is not running | Start Loom; Claude Desktop will retry |
| Requests show as `client_type: "api"` | Missing `x-loom-client` header (OAuth passthrough setup cannot send custom headers) | Expected under Setup A — the dashboard falls back to User-Agent detection |
| Claude Desktop ignores the config | Developer mode not enabled | Settings → Developer → enable Developer Mode, then restart |
