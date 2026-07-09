#!/usr/bin/env bash
# Test loom-oss (4444) as a drop-in replacement for the internal proxy (8711).
# Mimics exactly what Claude Code does: sends requests with x-api-key header.
#
# Usage:
#   ANTHROPIC_API_KEY=sk-... ./tests/test_proxy_cutover.sh
#   # or, if ANTHROPIC_API_KEY is already in your shell env:
#   ./tests/test_proxy_cutover.sh
#   # test against a different URL:
#   LOOM_TEST_URL=http://localhost:8711 ./tests/test_proxy_cutover.sh

set -euo pipefail

PROXY_URL="${LOOM_TEST_URL:-http://localhost:4444}"
API_KEY="${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY before running}"
MODEL="${LOOM_TEST_MODEL:-claude-haiku-4-5-20251001}"
PASS=0
FAIL=0
LOGDIR=$(mktemp -d)

green() { printf '\033[32m✓ %s\033[0m\n' "$1"; }
red()   { printf '\033[31m✗ %s\033[0m\n' "$1"; }
dim()   { printf '\033[2m  %s\033[0m\n' "$1"; }

debug_dump() {
  local label="$1" body_file="$2" headers_file="${3:-}"
  echo
  printf '\033[33m  ── DEBUG: %s ──\033[0m\n' "$label"
  if [ -n "$headers_file" ] && [ -f "$headers_file" ]; then
    echo "  Response headers:"
    sed 's/^/    /' "$headers_file"
  fi
  if [ -f "$body_file" ]; then
    local size
    size=$(wc -c < "$body_file")
    echo "  Response body (${size} bytes):"
    if python3 -c "import json; json.load(open('$body_file'))" 2>/dev/null; then
      python3 -c "import json; print(json.dumps(json.load(open('$body_file')), indent=2))" | head -40 | sed 's/^/    /'
    else
      head -40 "$body_file" | sed 's/^/    /'
    fi
    [ "$size" -gt 2000 ] && dim "... truncated (${size} bytes total)"
  fi
  echo
}

run_test() {
  local name="$1"
  shift
  local start_ns
  start_ns=$(date +%s%N 2>/dev/null || date +%s)
  local exit_code=0
  "$@" 2>&1 || exit_code=$?
  local end_ns
  end_ns=$(date +%s%N 2>/dev/null || date +%s)
  local elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))

  if [ "$exit_code" -eq 0 ]; then
    green "$name (${elapsed_ms}ms)"
    ((PASS++))
  else
    red "$name (${elapsed_ms}ms) — exit code $exit_code"
    ((FAIL++))
  fi
}

# ---- Test 1: Health check ----
test_health() {
  local body="$LOGDIR/health_body.json"
  local headers="$LOGDIR/health_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' "${PROXY_URL}/health")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code (expected 200)"
    debug_dump "health" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
errors = []
if d.get('status') != 'healthy':
    errors.append(f'status={d.get(\"status\")} (expected healthy)')
sess = d.get('sessions', {})
if not sess.get('supported'):
    errors.append(f'sessions.supported={sess.get(\"supported\")} (expected true)')
if not d.get('scanner_enabled'):
    errors.append(f'scanner_enabled={d.get(\"scanner_enabled\")} (expected true)')
if not d.get('routing_table_loaded'):
    errors.append(f'routing_table_loaded={d.get(\"routing_table_loaded\")} (expected true)')
if errors:
    print('  FAIL:', '; '.join(errors))
    print('  Full health response:')
    print(json.dumps(d, indent=2))
    sys.exit(1)
print(f'  version={d.get(\"version\")}, uptime={d.get(\"uptime_seconds\")}s')
print(f'  sessions.supported=true, scanner=true, routing=true')
print(f'  providers: {d.get(\"providers\", [])}')
print(f'  requests so far: {d.get(\"requests\", 0)}, errors: {d.get(\"errors\", 0)}')
" || { debug_dump "health" "$body" "$headers"; return 1; }
}

# ---- Test 2: count_tokens endpoint ----
test_count_tokens() {
  local body="$LOGDIR/count_tokens_body.json"
  local headers="$LOGDIR/count_tokens_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    -X POST "${PROXY_URL}/v1/messages/count_tokens" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "count_tokens" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
tokens = d.get('input_tokens', 0)
if tokens <= 0:
    print(f'  FAIL: input_tokens={tokens} (expected > 0)')
    print(f'  Response: {json.dumps(d, indent=2)}')
    sys.exit(1)
print(f'  input_tokens={tokens}')
" || { debug_dump "count_tokens" "$body" "$headers"; return 1; }
}

# ---- Test 3: Non-streaming messages ----
test_messages_sync() {
  local body="$LOGDIR/messages_sync_body.json"
  local headers="$LOGDIR/messages_sync_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    -X POST "${PROXY_URL}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -d "{\"model\":\"${MODEL}\",\"max_tokens\":50,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly the word: pong\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "messages_sync" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
if d.get('type') == 'error':
    err = d.get('error', {})
    print(f'  FAIL: API error: {err.get(\"type\")}: {err.get(\"message\")}')
    sys.exit(1)
if d.get('type') != 'message':
    print(f'  FAIL: unexpected type={d.get(\"type\")}')
    print(f'  Response: {json.dumps(d, indent=2)}')
    sys.exit(1)
content = d.get('content', [])
text = content[0].get('text', '') if content else ''
usage = d.get('usage', {})
model = d.get('model', '?')
print(f'  model={model}')
print(f'  text={repr(text[:120])}')
print(f'  usage: input={usage.get(\"input_tokens\",\"?\")}, output={usage.get(\"output_tokens\",\"?\")}')
stop = d.get('stop_reason', '?')
print(f'  stop_reason={stop}')
" || { debug_dump "messages_sync" "$body" "$headers"; return 1; }
}

# ---- Test 4: Streaming messages ----
test_messages_stream() {
  local body="$LOGDIR/messages_stream_body.txt"
  local headers="$LOGDIR/messages_stream_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    -X POST "${PROXY_URL}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -d "{\"model\":\"${MODEL}\",\"max_tokens\":30,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "messages_stream" "$body" "$headers"
    return 1
  fi

  local events
  events=$(grep -c '^event:' "$body" 2>/dev/null || echo 0)
  local has_start has_stop has_delta
  has_start=$(grep -c 'message_start' "$body" 2>/dev/null || echo 0)
  has_delta=$(grep -c 'content_block_delta' "$body" 2>/dev/null || echo 0)
  has_stop=$(grep -c 'message_stop' "$body" 2>/dev/null || echo 0)

  local errors=0
  if [ "$events" -lt 3 ]; then
    echo "  FAIL: only $events SSE events (expected ≥3)"
    errors=1
  fi
  if [ "$has_start" -eq 0 ]; then
    echo "  FAIL: no message_start event"
    errors=1
  fi
  if [ "$has_stop" -eq 0 ]; then
    echo "  FAIL: no message_stop event"
    errors=1
  fi

  if [ "$errors" -gt 0 ]; then
    debug_dump "messages_stream" "$body" "$headers"
    return 1
  fi

  echo "  events: ${events} total (start=$has_start, deltas=$has_delta, stop=$has_stop)"
}

# ---- Test 5: Session recorded in Postgres ----
test_session_recorded() {
  sleep 2
  local body="$LOGDIR/sessions_body.json"
  local headers="$LOGDIR/sessions_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    "${PROXY_URL}/api/sessions?hours=1")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "sessions" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
sessions = d if isinstance(d, list) else d.get('sessions', [])
if len(sessions) == 0:
    print('  FAIL: no sessions recorded in Postgres after test API calls')
    print(f'  Response: {json.dumps(d, indent=2)[:500]}')
    sys.exit(1)
print(f'  {len(sessions)} session(s) recorded in last hour')
latest = sessions[0] if sessions else {}
for k in ('session_id', 'model', 'turns', 'created_at'):
    if k in latest:
        print(f'    latest.{k} = {latest[k]}')
" || { debug_dump "sessions" "$body" "$headers"; return 1; }
}

# ---- Test 6: Compression is functional ----
test_compression_active() {
  local body="$LOGDIR/compression_body.json"
  local http_code
  http_code=$(curl -s -o "$body" -w '%{http_code}' "${PROXY_URL}/health")

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
c = d.get('compression', {})
if not c.get('enabled'):
    print(f'  FAIL: compression.enabled={c.get(\"enabled\")}')
    print(f'  Full compression block: {json.dumps(c, indent=2)}')
    sys.exit(1)
print(f'  enabled=true, tier={c.get(\"default_tier\",\"?\")}')
print(f'  tokens: before={c.get(\"tokens_before\",0)}, after={c.get(\"tokens_after\",0)}, saved={c.get(\"tokens_saved\",0)}')
print(f'  ratio={c.get(\"compression_ratio\",0)}')
" || return 1
}

# ---- Test 7: Governor endpoint accessible ----
test_governor() {
  local body="$LOGDIR/governor_body.json"
  local headers="$LOGDIR/governor_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    "${PROXY_URL}/api/governor")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "governor" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
if 'tier_thresholds' not in d:
    print(f'  FAIL: governor response missing tier_thresholds')
    print(f'  Response: {json.dumps(d, indent=2)}')
    sys.exit(1)
print(f'  enabled={d.get(\"enabled\")}')
print(f'  thresholds={d.get(\"tier_thresholds\")}')
if d.get('class_overrides'):
    print(f'  class_overrides={d.get(\"class_overrides\")}')
" || { debug_dump "governor" "$body" "$headers"; return 1; }
}

# ---- Test 8: Compare with internal proxy (if reachable) ----
test_parity_check() {
  local old_url="http://localhost:8711"
  local old_body="$LOGDIR/parity_old.json"
  local new_body="$LOGDIR/parity_new.json"

  local old_code
  old_code=$(curl -s -o "$old_body" -w '%{http_code}' "${old_url}/health" 2>/dev/null) || old_code="000"

  if [ "$old_code" != "200" ]; then
    echo "  skipped: internal proxy at $old_url not reachable (HTTP $old_code)"
    return 0
  fi

  curl -s -o "$new_body" "${PROXY_URL}/health"

  python3 -c "
import sys, json
with open('$old_body') as f:
    old = json.load(f)
with open('$new_body') as f:
    new = json.load(f)

print('  Feature parity comparison (8711 vs 4444):')
checks = [
    ('compression.enabled', old.get('compression',{}).get('enabled'), new.get('compression',{}).get('enabled')),
    ('scanner.enabled', old.get('scanner',{}).get('enabled'), new.get('scanner_enabled')),
    ('sessions.supported', True, new.get('sessions',{}).get('supported')),
]
all_ok = True
for label, old_val, new_val in checks:
    match = '✓' if old_val == new_val else '✗ MISMATCH'
    if old_val != new_val:
        all_ok = False
    print(f'    {label}: 8711={old_val} → 4444={new_val} {match}')

# Show what 8711 has that 4444 doesn't report
old_keys = set()
def collect_keys(d, prefix=''):
    for k, v in d.items():
        full = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            collect_keys(v, full)
        else:
            old_keys.add(full)
collect_keys(old)
new_keys = set()
collect_keys(new)

missing = old_keys - new_keys
if missing:
    print(f'  Health fields in 8711 but not in 4444:')
    for m in sorted(missing):
        print(f'    - {m}')

if not all_ok:
    sys.exit(1)
"
}

echo "============================================"
echo " Loom-OSS Proxy Cutover Test"
echo " Target:  ${PROXY_URL}"
echo " Model:   ${MODEL}"
echo " Log dir: ${LOGDIR}"
echo "============================================"
echo

run_test "Health check" test_health
run_test "count_tokens" test_count_tokens
run_test "Messages (sync)" test_messages_sync
run_test "Messages (stream)" test_messages_stream
run_test "Session recorded" test_session_recorded
run_test "Compression active" test_compression_active
run_test "Governor endpoint" test_governor
run_test "Parity check vs 8711" test_parity_check

echo
echo "============================================"
printf 'Results: \033[32m%d passed\033[0m' "$PASS"
[ "$FAIL" -gt 0 ] && printf ', \033[31m%d failed\033[0m' "$FAIL"
echo
echo "Log dir: ${LOGDIR}"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
  echo
  echo "Inspect debug logs in: ${LOGDIR}/"
  echo "  ls ${LOGDIR}/"
  echo
  echo "Fix failures before switching ANTHROPIC_BASE_URL in settings.json:"
  echo "  ~/.claude/settings.json → env.ANTHROPIC_BASE_URL → ${PROXY_URL}"
  exit 1
else
  echo
  echo "All tests passed. Safe to update settings.json:"
  echo '  "ANTHROPIC_BASE_URL": "'${PROXY_URL}'"'
  exit 0
fi
