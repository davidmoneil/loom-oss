# Investigation: Compression Flattening tool_use/tool_result → 400 Errors

**Date**: 2026-07-10
**Status**: Fixed (PR #11)
**Severity**: High — broke all Claude Code sessions using parallel tool calls through Loom
**Related**: Cutover bugs #1–3 (header passthrough, fixed 2026-07-09)

---

## Symptom

Claude Code sessions routed through loom-oss (`:4444`) intermittently returned:

```
API Error: 400 — tool use concurrency issues
```

The errors appeared when Claude Code spawned subagents or used parallel tool calls (Agent tool, Workflow tool, background jobs). Single-tool-call turns worked fine.

## Root Cause

`_compress_messages_inline()` in `src/loom/gateway/app.py` (line 2046) iterates over conversation messages and compresses older ones to save tokens. The function:

1. Extracts `msg.get("content", "")` from each message
2. If content is a string → compresses directly
3. If content is a list → falls through to `json.dumps(content)` at line 2089, converting the structured list to a flat JSON string
4. The compressed/stringified result is assigned back as `msg["content"]`

For messages containing `tool_use` or `tool_result` content blocks, the Anthropic API **requires** content to remain a structured list:

```json
{
  "role": "assistant",
  "content": [
    {"type": "text", "text": "Let me check that."},
    {"type": "tool_use", "id": "toolu_01", "name": "bash", "input": {"cmd": "ls"}}
  ]
}
```

After compression flattened this to a string, Anthropic rejected it with HTTP 400.

### Why intermittent?

- Only messages old enough to be compression-eligible (not in the last 2 turns) are affected
- Only messages with list-type content containing tool blocks trigger the bug
- In short conversations or conversations without tool use, the bug never fires
- Parallel tool calls produce more tool_use/tool_result messages, making older ones eligible sooner

## Investigation Steps

This section documents the full diagnostic path for future reference.

### 1. Log inspection

**Docker container logs** (`docker logs loom-oss-loom-1`):
- Post-fix: only 200s (health checks, API calls) — no 400s
- Pre-fix: the 400s appeared as `upstream 400` errors logged by `AnthropicBackend._complete()` (line 187) and `._stream()` (line 219)

**Audit log** (`logs/audit.jsonl`):
- Contains structured records with `status_code` field for every request
- Post-fix: 6 entries, zero 400s from Anthropic (one 401 from unauthenticated `/v1/messages` call)
- The audit log rotates, so pre-fix entries were no longer available

**Metrics log** (`logs/metrics.jsonl`):
- 5 entries, all successful Ollama requests
- Metrics table in SQLite (`data/loom.db`) lacks a `status_code` column — only records successful requests

### 2. Database schema check

SQLite tables in `data/loom.db`:
- `metrics` — tokens, latency, cost, compression ratio (no error tracking)
- `routing_decisions` — model routing logic
- `sessions` — conversation fingerprints
- `compression_cache` — cached compressed text by content hash

The database does **not** record failed requests. Error visibility depends entirely on audit.jsonl and Docker logs.

### 3. Code path trace

The request flow for `/v1/messages`:

```
app.py:messages_endpoint()
  → _compress_messages_inline()     ← BUG HERE (line 2046)
  → backend.chat_completion()
  → AnthropicBackend._complete()    ← 400 response caught here
  → ProviderError raised
  → _audit_error() records it
  → _error_response() returns to client
```

The compression step runs **before** the request is sent upstream. By the time the 400 comes back from Anthropic, the message content has already been corrupted from a structured list to a string.

### 4. Affected code location

```
src/loom/gateway/app.py
  Lines 2046–2142: _compress_messages_inline()
  Line 2072:       content = msg.get("content", "")
  Line 2089:       text = content if isinstance(content, str) else json.dumps(content)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                   This line flattens list content (including tool blocks) to a string
```

### 5. Fix verification

**The fix** (lines 2074–2087, PR #11):

```python
if isinstance(content, list):
    if any(
        isinstance(block, dict)
        and block.get("type") in ("tool_use", "tool_result")
        for block in content
    ):
        tb = _estimate_tokens_safe(json.dumps(content))
        tokens_before += tb
        tokens_after += tb
        compressed.append(msg)
        continue
```

Messages whose content contains any `tool_use` or `tool_result` block are skipped entirely and passed through untouched. Token accounting still counts them (so compression ratios are accurate), but no transformation is applied.

**Test coverage**: `tests/test_compression_inline.py::test_tool_use_messages_preserved` — verifies both tool_use (assistant) and tool_result (user) messages survive compression intact.

**Runtime verification** (2026-07-10):
- Docker logs: zero 400s in last hour, all requests returning 200
- Audit log: no Anthropic errors
- Claude Code sessions with parallel tool calls working normally through loom

## Known Edge Case

Content that is a list of **plain text blocks** (no tool_use/tool_result) still gets `json.dumps()`-ed to a string at line 2089. Example:

```json
[{"type": "text", "text": "hello"}]
```

becomes the literal string `'[{"type":"text","text":"hello"}]'`.

Anthropic interprets this as raw text, not structured content — so it doesn't cause a 400, but it does change the message semantics. In practice, Claude Code sends plain text as strings (not lists), so this path rarely fires. However, `image` content blocks could theoretically hit this path if they appear in compression-eligible messages. This is a latent issue, not an active bug.

## Lessons Learned

1. **List-type content in the Anthropic API is structural, not cosmetic.** `tool_use`, `tool_result`, and `image` blocks must remain as lists — converting to strings changes API semantics.

2. **Compression must be content-type-aware.** A blanket `json.dumps()` fallback for non-string content is dangerous when downstream APIs distinguish between strings and structured lists.

3. **Intermittent 400s from upstream APIs often indicate request mutation, not rate limiting.** When the proxy modifies request bodies (compression, normalization), the mutation itself can be the bug.

4. **Error recording gap.** The SQLite `metrics` table only records successes. Failed requests are only visible in `audit.jsonl` (which rotates) and Docker logs (also limited). Consider adding error tracking to the database for post-mortem analysis.

## Files Referenced

| File | Purpose |
|------|---------|
| `src/loom/gateway/app.py` | Gateway routes, `_compress_messages_inline()` |
| `src/loom/gateway/providers/anthropic.py` | `AnthropicBackend._complete()`, `._stream()` — where 400s surface |
| `tests/test_compression_inline.py` | Unit tests including `test_tool_use_messages_preserved` |
| `logs/audit.jsonl` | Per-request audit trail with status codes |
| `data/loom.db` | SQLite metrics (successes only) |

## Diagnostic Checklist (for future 400 investigations)

1. `docker logs loom-oss-loom-1 2>&1 | grep -i "400\|error"` — check for upstream rejections
2. `cat logs/audit.jsonl | python3 -m json.tool` — structured audit with status codes
3. Check `_compress_messages_inline()` — is it mutating content structure?
4. Check `AnthropicBackend._headers()` — duplicate/missing headers?
5. Check `_upstream_url()` — is the query string leaking through?
6. For Anthropic-specific 400s, add `_log.error()` in `_complete()` to dump the request body (already present post-cutover debugging)
7. Python3 one-liner for SQLite: `python3 -c "import sqlite3; conn=sqlite3.connect('data/loom.db'); ..."` (sqlite3 CLI not available on host)
