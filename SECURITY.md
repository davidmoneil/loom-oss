# Security Policy

## Reporting a vulnerability

Please open a [private security advisory](https://github.com/davidmoneil/loom-oss/security/advisories/new)
rather than a public issue. You should receive a response within a week.

## Security model

Loom is a gateway that sits between LLM clients and providers. It sees every
prompt, response, and provider credential that passes through it — treat the
host it runs on accordingly.

### Authentication

- With **no gateway keys provisioned**, the gateway runs **open**: every API
  and inference endpoint accepts unauthenticated requests. This is intended
  only for first-run setup on a trusted network. The state is reported loudly:
  a `CRITICAL` startup log line, `auth_enabled: false` in `/health`, and a red
  banner in the dashboard.
- Once at least one key exists (dashboard → Settings → API Access, or
  `POST /api/config/gateway-keys`), all `/api/*` and `/v1/*` routes require a
  key via the `x-loom-gateway-key` header (also accepted: `x-api-key` or a
  Bearer token). Public exceptions: `/health`, `/v1/models`, `/api/models`,
  `/api/tags`, docs, and the dashboard's static assets.
- Keys are stored as SHA-256 hashes; the full key is returned exactly once at
  creation.
- With `server.oauth_passthrough` enabled, requests bearing an Anthropic OAuth
  token (`sk-ant-oat...`) bypass gateway-key auth on `/v1/*` inference routes
  only — the token is validated upstream by Anthropic. It never grants access
  to the admin API.

### Deployment guidance

- Bind to loopback (`server.host: 127.0.0.1`) unless remote clients need the
  gateway; the default bind is `0.0.0.0`.
- Put TLS in front (reverse proxy) before exposing the gateway beyond
  localhost — gateway keys travel in headers.
- The compression LLM endpoint (`compression.llm_url`) may only point at
  loopback by default; private/LAN addresses require
  `compression.allow_private_llm_url: true`. Public URLs are allowed, but the
  URL is validated (scheme + resolved address) at config time and again at
  call time to prevent SSRF via runtime config changes.
- Content logging (`scanner.content_logging`) writes full prompts/responses to
  disk. Leave it `off` unless you need audit content, and protect the log
  directory.
