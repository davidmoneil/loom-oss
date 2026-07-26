# Using Loom with Claude Desktop (Developer Mode)

Route Claude Desktop's API traffic through Loom for compression and observability.
Requires **Developer Mode** (Settings → Developer).

## How It Works

Claude Desktop in developer mode lets you configure a custom base URL for Anthropic API
calls. Point it at Loom, and all chat traffic flows through the gateway. Because Claude
Desktop sends its own API key in the `Authorization` header, you need a **separate**
gateway key (`x-loom-gateway-key`) if Loom has key authentication enabled — otherwise the
`Authorization` bearer token would be consumed by gateway auth and never reach the
upstream provider.

## Prerequisites

- Loom running locally (default port `4444`)
- Claude Desktop with Developer Mode enabled (Settings → Developer)
- A gateway key, if Loom has key authentication enabled

## Setup

### 1. Create a gateway key (if key auth is enabled)

From the Loom dashboard (Settings → Gateway Keys) or via the API:

```bash
curl -s http://localhost:4444/api/gateway-keys \
  -H 'Content-Type: application/json' \
  -d '{"name": "claude-desktop"}' | jq .
```

Save the returned key — it is shown only once.

If you're running Loom without gateway key auth (no keys configured), skip this step.

### 2. Configure Claude Desktop

Open Claude Desktop → Settings → Developer → Edit Config, and add:

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

### 3. (Optional) Add a source policy

In `loom.yaml`, add a source-specific configuration:

```yaml
sources:
  desktop:
    tier: light           # compression tier
    model_preference:
      - claude-sonnet-4-20250514
```

Without a dedicated source policy, requests from Claude Desktop use the `default` source.

## How Gateway Key Auth Works with Claude Desktop

Without the `x-loom-gateway-key` header:

1. Claude Desktop sends `Authorization: Bearer sk-ant-...` (its Anthropic API key)
2. Loom's gateway auth middleware reads the `Authorization` header
3. The bearer token is validated as a gateway key — **it fails** because it's an Anthropic key, not a gateway key
4. Request is rejected with 401

With the `x-loom-gateway-key` header:

1. Claude Desktop sends both `Authorization: Bearer sk-ant-...` and `x-loom-gateway-key: gk-...`
2. Loom's auth middleware reads `x-loom-gateway-key` first — validates it as a gateway key
3. The `Authorization` header passes through untouched to the upstream provider
4. Anthropic receives the original API key and authenticates normally

The `x-loom-gateway-key` header is stripped before forwarding to upstream providers.

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
| 401 Unauthorized | Gateway key auth is on, no `x-loom-gateway-key` header | Add the header to `defaultHeaders` in Claude Desktop config |
| Connection refused | Loom is not running | Start Loom; Claude Desktop will retry |
| Requests show as `client_type: "api"` | Missing `x-loom-client` header | Add `"x-loom-client": "claude-desktop"` to `defaultHeaders` |
| Claude Desktop ignores the config | Developer mode not enabled | Settings → Developer → enable Developer Mode, then restart |
