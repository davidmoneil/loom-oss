# Using Loom with Anthropic Claude Code CLI

Route Claude Code's API traffic through Loom for transparent compression and observability.
This applies to interactive sessions, background jobs, and `claude agents`.

## How It Works

Claude Code uses the `ANTHROPIC_BASE_URL` environment variable to determine where to send
Anthropic API requests. By pointing it at a local Loom instance, all API traffic flows
through Loom's compression layer before reaching `api.anthropic.com`. Auth headers (Max
OAuth, API keys) pass through untouched.

**What gets routed**: Only Anthropic SDK API calls (`/v1/messages`).
**What is NOT affected**: MCP servers, git, npm, web fetches, file operations, Bash — everything else uses its own transport.

## Prerequisites

- Loom running locally (default port `4000`, or configure as needed)
- Claude Code CLI installed

## Setup

### 1. Add the environment variable to Claude Code settings

Edit `~/.claude/settings.json` and add `ANTHROPIC_BASE_URL` to the `env` section:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4000"
  }
}
```

Replace `4000` with your Loom port if different.

**Important**:
- Use `localhost`, not a LAN IP — Loom binds locally
- Do NOT set `HTTPS_PROXY` — that routes ALL HTTPS traffic and will break MCP, git, npm, and everything else
- `settings.local.json` env vars do NOT propagate to Claude Code worker processes — use `settings.json`

### 2. (Recommended) Add a health-check hook

Create `~/.claude/hooks/loom-health-check.sh`:

```bash
#!/usr/bin/env bash
# Warn if Loom is down — API calls will fail since ANTHROPIC_BASE_URL points there
LOOM_PORT="${LOOM_PORT:-4000}"
if ! curl -sf --max-time 2 "http://localhost:${LOOM_PORT}/health" >/dev/null 2>&1; then
    cat <<EOF
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"WARNING: Loom compression proxy (localhost:${LOOM_PORT}) is unreachable. API calls will fail. Fix: restart Loom or temporarily remove ANTHROPIC_BASE_URL from ~/.claude/settings.json"}}
EOF
fi
```

```bash
chmod +x ~/.claude/hooks/loom-health-check.sh
```

Register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/loom-health-check.sh",
            "timeout": 3000
          }
        ]
      }
    ]
  }
}
```

### 3. (Recommended) Add a startup guard

Create `~/.claude/hooks/loom-env-guard.sh`:

```bash
#!/usr/bin/env bash
# SessionStart guard: warn if ANTHROPIC_BASE_URL was removed from settings
LOOM_PORT="${LOOM_PORT:-4000}"
if [ -z "$ANTHROPIC_BASE_URL" ] || ! echo "$ANTHROPIC_BASE_URL" | grep -q "localhost:${LOOM_PORT}"; then
    cat <<EOF
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"WARNING: ANTHROPIC_BASE_URL is not set to http://localhost:${LOOM_PORT}. Sessions are NOT routing through Loom. To fix: add ANTHROPIC_BASE_URL to ~/.claude/settings.json env section."}}
EOF
fi
```

Register in `~/.claude/settings.json` under `SessionStart`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/loom-env-guard.sh",
            "timeout": 3000
          }
        ]
      }
    ]
  }
}
```

## Verify

```bash
# Note the request count
curl -sf http://localhost:4000/health | jq .requests

# Start a new Claude Code session and send a message
claude

# Check the count increased
curl -sf http://localhost:4000/health | jq .requests
```

New sessions pick up settings.json changes at process start. Existing sessions
won't be affected until restarted.

## Headless / CI Sessions

If you also run Claude Code headless (e.g., via a job executor), the executor can
`export ANTHROPIC_BASE_URL` before launching `claude -p`, which overrides the
settings.json value. This lets headless sessions use a different Loom instance or
disable routing independently.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| API calls fail with connection error | Loom is down | Restart Loom or remove `ANTHROPIC_BASE_URL` from settings.json |
| Setting disappears after git operations | settings.json is git-tracked | Re-add the env var; the SessionStart guard hook will warn you |
| MCP/git/npm broken | `HTTPS_PROXY` was set | Remove `HTTPS_PROXY` — only use `ANTHROPIC_BASE_URL` |
| Setting works in daemon but not workers | Set in settings.local.json | Move to settings.json — local settings env doesn't propagate |
