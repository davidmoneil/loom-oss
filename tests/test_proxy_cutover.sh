#!/usr/bin/env bash
# Test loom-oss (4444) as a drop-in replacement for the internal proxy (8711).
#
# Runs infrastructure tests (no key needed), then exercises each provider
# whose API key is available. Ollama is always free/local.
#
# Usage:
#   ./tests/test_proxy_cutover.sh                          # infra + ollama
#   ANTHROPIC_API_KEY=sk-... ./tests/test_proxy_cutover.sh # + anthropic
#   OPENAI_API_KEY=sk-... ./tests/test_proxy_cutover.sh    # + openai
#   GEMINI_API_KEY=... ./tests/test_proxy_cutover.sh       # + gemini

set -euo pipefail

PROXY_URL="${LOOM_TEST_URL:-http://localhost:4444}"
OLLAMA_MODEL="${LOOM_OLLAMA_MODEL:-qwen3:1.7b}"
ANTHROPIC_MODEL="${LOOM_ANTHROPIC_MODEL:-claude-haiku-4-5-20251001}"
OPENAI_MODEL="${LOOM_OPENAI_MODEL:-gpt-4o-mini}"
GEMINI_MODEL="${LOOM_GEMINI_MODEL:-gemini-2.0-flash}"
PASS=0
FAIL=0
SKIP=0
LOGDIR=$(mktemp -d)

green() { printf '\033[32m✓ %s\033[0m\n' "$1"; }
red()   { printf '\033[31m✗ %s\033[0m\n' "$1"; }
yellow(){ printf '\033[33m⊘ %s\033[0m\n' "$1"; }
dim()   { printf '\033[2m  %s\033[0m\n' "$1"; }
header(){ printf '\n\033[1;36m── %s ──\033[0m\n' "$1"; }

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
      python3 -c "import json; print(json.dumps(json.load(open('$body_file')), indent=2))" | head -50 | sed 's/^/    /'
    else
      head -50 "$body_file" | sed 's/^/    /'
    fi
    [ "$size" -gt 3000 ] && dim "... truncated (${size} bytes total)"
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
    PASS=$((PASS + 1))
  else
    red "$name (${elapsed_ms}ms) — exit code $exit_code"
    FAIL=$((FAIL + 1))
  fi
}

skip_test() {
  local name="$1" reason="$2"
  yellow "$name — $reason"
  SKIP=$((SKIP + 1))
}

###############################################################################
# INFRASTRUCTURE TESTS (no API key needed)
###############################################################################

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
print(f'  version={d.get(\"version\")}, uptime={d.get(\"uptime_seconds\",\"?\")}s')
print(f'  sessions.supported=true, scanner=true, routing=true')
print(f'  providers: {d.get(\"providers\", [])}')
print(f'  requests so far: {d.get(\"requests\", 0)}, errors: {d.get(\"errors\", 0)}')
" || { debug_dump "health" "$body" "$headers"; return 1; }
}

test_compression_active() {
  local body="$LOGDIR/compression_body.json"
  curl -s -o "$body" "${PROXY_URL}/health"

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

test_governor() {
  local body="$LOGDIR/governor_body.json"
  local headers="$LOGDIR/governor_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' "${PROXY_URL}/api/governor")

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

print('  Feature parity (8711 vs 4444):')
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
    print(f'  Health fields in 8711 but not 4444:')
    for m in sorted(missing):
        print(f'    - {m}')

if not all_ok:
    sys.exit(1)
"
}

###############################################################################
# OLLAMA TESTS (free, local)
###############################################################################

test_ollama_chat_sync() {
  local body="$LOGDIR/ollama_chat_sync_body.json"
  local headers="$LOGDIR/ollama_chat_sync_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    --max-time 60 \
    -X POST "${PROXY_URL}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${OLLAMA_MODEL}\",\"max_tokens\":40,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly the word: pong\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "ollama_chat_sync" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
if 'error' in d:
    print(f'  FAIL: {json.dumps(d[\"error\"])}')
    sys.exit(1)
choices = d.get('choices', [])
if not choices:
    print(f'  FAIL: no choices in response')
    print(f'  Response: {json.dumps(d, indent=2)[:500]}')
    sys.exit(1)
msg = choices[0].get('message', {})
text = msg.get('content', '')
model = d.get('model', '?')
usage = d.get('usage', {})
comp = usage.get('completion_tokens', 0)
if comp <= 0 and not text:
    print(f'  FAIL: no content and completion_tokens={comp}')
    print(f'  Response: {json.dumps(d, indent=2)[:500]}')
    sys.exit(1)
print(f'  model={model}')
print(f'  text={repr(text[:120]) if text else \"(empty — thinking model, content in <think> tags)\"}')
print(f'  usage: prompt={usage.get(\"prompt_tokens\",\"?\")}, completion={comp}, total={usage.get(\"total_tokens\",\"?\")}')
print(f'  finish_reason={choices[0].get(\"finish_reason\",\"?\")}')
" || { debug_dump "ollama_chat_sync" "$body" "$headers"; return 1; }
}

test_ollama_chat_stream() {
  local body="$LOGDIR/ollama_chat_stream_body.txt"
  local headers="$LOGDIR/ollama_chat_stream_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    --max-time 60 \
    -X POST "${PROXY_URL}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${OLLAMA_MODEL}\",\"max_tokens\":30,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "ollama_chat_stream" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
lines = open('$body').readlines()
if not lines:
    print('  FAIL: empty response body')
    sys.exit(1)

# Detect format: SSE (data: prefix) vs NDJSON (raw JSON lines)
sse_lines = [l for l in lines if l.strip().startswith('data:')]
if sse_lines:
    data_count = len(sse_lines)
    has_done = any('[DONE]' in l for l in lines)
    print(f'  format=SSE, data lines={data_count}, [DONE]={has_done}')
    if data_count < 2:
        print(f'  FAIL: only {data_count} data lines')
        sys.exit(1)
    if not has_done:
        print('  FAIL: no [DONE] sentinel')
        sys.exit(1)
else:
    # NDJSON: each line is a JSON object, last has done=true
    json_lines = [l.strip() for l in lines if l.strip()]
    if len(json_lines) < 2:
        print(f'  FAIL: only {len(json_lines)} NDJSON lines')
        sys.exit(1)
    last = json.loads(json_lines[-1])
    has_done = last.get('done', False)
    print(f'  format=NDJSON, lines={len(json_lines)}, done={has_done}')
    if not has_done:
        print('  FAIL: last line missing done=true')
        sys.exit(1)
" || { debug_dump "ollama_chat_stream" "$body" "$headers"; return 1; }
}

test_ollama_native() {
  local body="$LOGDIR/ollama_native_body.json"
  local headers="$LOGDIR/ollama_native_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    --max-time 60 \
    -X POST "${PROXY_URL}/api/generate" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${OLLAMA_MODEL}\",\"prompt\":\"Say pong\",\"stream\":false}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "ollama_native" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
if 'error' in d:
    print(f'  FAIL: {d[\"error\"]}')
    sys.exit(1)
text = d.get('response', '')
model = d.get('model', '?')
done = d.get('done', False)
print(f'  model={model}')
print(f'  response={repr(text[:120])}')
print(f'  done={done}')
if d.get('eval_count'):
    print(f'  eval_count={d[\"eval_count\"]}, eval_duration={d.get(\"eval_duration\",0)/1e9:.2f}s')
" || { debug_dump "ollama_native" "$body" "$headers"; return 1; }
}

###############################################################################
# ANTHROPIC TESTS (requires ANTHROPIC_API_KEY)
###############################################################################

test_anthropic_count_tokens() {
  local body="$LOGDIR/anthropic_count_tokens_body.json"
  local headers="$LOGDIR/anthropic_count_tokens_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    -X POST "${PROXY_URL}/v1/messages/count_tokens" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -d "{\"model\":\"${ANTHROPIC_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "anthropic_count_tokens" "$body" "$headers"
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
" || { debug_dump "anthropic_count_tokens" "$body" "$headers"; return 1; }
}

test_anthropic_messages_sync() {
  local body="$LOGDIR/anthropic_sync_body.json"
  local headers="$LOGDIR/anthropic_sync_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    -X POST "${PROXY_URL}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -d "{\"model\":\"${ANTHROPIC_MODEL}\",\"max_tokens\":50,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: pong\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "anthropic_messages_sync" "$body" "$headers"
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
content = d.get('content', [])
text = content[0].get('text', '') if content else ''
usage = d.get('usage', {})
print(f'  model={d.get(\"model\",\"?\")}')
print(f'  text={repr(text[:120])}')
print(f'  usage: input={usage.get(\"input_tokens\",\"?\")}, output={usage.get(\"output_tokens\",\"?\")}')
print(f'  stop_reason={d.get(\"stop_reason\",\"?\")}')
" || { debug_dump "anthropic_messages_sync" "$body" "$headers"; return 1; }
}

test_anthropic_messages_stream() {
  local body="$LOGDIR/anthropic_stream_body.txt"
  local headers="$LOGDIR/anthropic_stream_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    -X POST "${PROXY_URL}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -d "{\"model\":\"${ANTHROPIC_MODEL}\",\"max_tokens\":30,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "anthropic_stream" "$body" "$headers"
    return 1
  fi

  local events has_start has_stop
  events=$(grep -c '^event:' "$body" 2>/dev/null || echo 0)
  has_start=$(grep -c 'message_start' "$body" 2>/dev/null || echo 0)
  has_stop=$(grep -c 'message_stop' "$body" 2>/dev/null || echo 0)

  local errors=0
  [ "$events" -lt 3 ] && { echo "  FAIL: only $events SSE events"; errors=1; }
  [ "$has_start" -eq 0 ] && { echo "  FAIL: no message_start"; errors=1; }
  [ "$has_stop" -eq 0 ] && { echo "  FAIL: no message_stop"; errors=1; }

  if [ "$errors" -gt 0 ]; then
    debug_dump "anthropic_stream" "$body" "$headers"
    return 1
  fi

  echo "  events: $events total (start=$has_start, stop=$has_stop)"
}

###############################################################################
# OPENAI TESTS (requires OPENAI_API_KEY)
###############################################################################

test_openai_chat_sync() {
  local body="$LOGDIR/openai_chat_sync_body.json"
  local headers="$LOGDIR/openai_chat_sync_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    -X POST "${PROXY_URL}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${OPENAI_API_KEY}" \
    -d "{\"model\":\"${OPENAI_MODEL}\",\"max_tokens\":40,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: pong\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "openai_chat_sync" "$body" "$headers"
    return 1
  fi

  python3 -c "
import sys, json
with open('$body') as f:
    d = json.load(f)
if 'error' in d:
    print(f'  FAIL: {d[\"error\"]}')
    sys.exit(1)
choices = d.get('choices', [])
if not choices:
    print(f'  FAIL: no choices')
    print(f'  Response: {json.dumps(d, indent=2)[:500]}')
    sys.exit(1)
text = choices[0].get('message',{}).get('content','')
usage = d.get('usage', {})
print(f'  model={d.get(\"model\",\"?\")}')
print(f'  text={repr(text[:120])}')
print(f'  usage: prompt={usage.get(\"prompt_tokens\",\"?\")}, completion={usage.get(\"completion_tokens\",\"?\")}, total={usage.get(\"total_tokens\",\"?\")}')
" || { debug_dump "openai_chat_sync" "$body" "$headers"; return 1; }
}

test_openai_chat_stream() {
  local body="$LOGDIR/openai_chat_stream_body.txt"
  local headers="$LOGDIR/openai_chat_stream_headers.txt"
  local http_code
  http_code=$(curl -s -o "$body" -D "$headers" -w '%{http_code}' \
    --max-time 30 \
    -X POST "${PROXY_URL}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${OPENAI_API_KEY}" \
    -d "{\"model\":\"${OPENAI_MODEL}\",\"max_tokens\":30,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}]}")

  if [ "$http_code" != "200" ]; then
    echo "  FAIL: HTTP $http_code"
    debug_dump "openai_chat_stream" "$body" "$headers"
    return 1
  fi

  local data_lines done_count
  data_lines=$(grep -c '^data:' "$body" 2>/dev/null || echo 0)
  done_count=$(grep -c '\[DONE\]' "$body" 2>/dev/null || echo 0)

  [ "$data_lines" -lt 2 ] && { echo "  FAIL: only $data_lines data lines"; debug_dump "openai_chat_stream" "$body" "$headers"; return 1; }
  [ "$done_count" -eq 0 ] && { echo "  FAIL: no [DONE]"; debug_dump "openai_chat_stream" "$body" "$headers"; return 1; }

  echo "  data lines: $data_lines, [DONE] received"
}

###############################################################################
# SESSION PERSISTENCE TEST (runs after provider tests)
###############################################################################

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
entries = d.get('entries', d if isinstance(d, list) else [])
count = d.get('sessions', len(entries)) if isinstance(d, dict) else len(d)
if not entries and count == 0:
    print('  FAIL: no sessions recorded in Postgres after test API calls')
    print(f'  Response: {json.dumps(d, indent=2)[:500]}')
    sys.exit(1)
print(f'  {count} session(s), {d.get(\"total_turns\", \"?\")} total turns in last hour')
for s in entries[:3]:
    sid = s.get('session_id', '?')[:16]
    print(f'    {sid}  source={s.get(\"source\",\"?\")}  turns={s.get(\"turns\",\"?\")}')
" || { debug_dump "sessions" "$body" "$headers"; return 1; }
}

###############################################################################
# RUN
###############################################################################

echo "============================================"
echo " Loom-OSS Proxy Cutover Test"
echo " Target:  ${PROXY_URL}"
echo " Log dir: ${LOGDIR}"
echo "============================================"
echo
echo " Available providers:"
echo "   Ollama:    ${OLLAMA_MODEL} (local, free)"
[ -n "${ANTHROPIC_API_KEY:-}" ] && echo "   Anthropic: ${ANTHROPIC_MODEL}" || echo "   Anthropic: (skipped — no ANTHROPIC_API_KEY)"
[ -n "${OPENAI_API_KEY:-}" ]    && echo "   OpenAI:    ${OPENAI_MODEL}"    || echo "   OpenAI:    (skipped — no OPENAI_API_KEY)"
[ -n "${GEMINI_API_KEY:-}" ]    && echo "   Gemini:    ${GEMINI_MODEL}"    || echo "   Gemini:    (skipped — no GEMINI_API_KEY)"

# ---- Infrastructure ----
header "Infrastructure"
run_test "Health check" test_health
run_test "Compression active" test_compression_active
run_test "Governor endpoint" test_governor
run_test "Parity check vs 8711" test_parity_check

# ---- Ollama (always runs) ----
header "Ollama (${OLLAMA_MODEL})"
run_test "Ollama chat/completions (sync)" test_ollama_chat_sync
run_test "Ollama chat/completions (stream)" test_ollama_chat_stream
run_test "Ollama native /api/generate" test_ollama_native

# ---- Anthropic (if key available) ----
header "Anthropic (${ANTHROPIC_MODEL})"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  run_test "Anthropic count_tokens" test_anthropic_count_tokens
  run_test "Anthropic messages (sync)" test_anthropic_messages_sync
  run_test "Anthropic messages (stream)" test_anthropic_messages_stream
else
  skip_test "Anthropic count_tokens" "no ANTHROPIC_API_KEY"
  skip_test "Anthropic messages (sync)" "no ANTHROPIC_API_KEY"
  skip_test "Anthropic messages (stream)" "no ANTHROPIC_API_KEY"
fi

# ---- OpenAI (if key available) ----
header "OpenAI (${OPENAI_MODEL})"
if [ -n "${OPENAI_API_KEY:-}" ]; then
  run_test "OpenAI chat/completions (sync)" test_openai_chat_sync
  run_test "OpenAI chat/completions (stream)" test_openai_chat_stream
else
  skip_test "OpenAI chat/completions (sync)" "no OPENAI_API_KEY"
  skip_test "OpenAI chat/completions (stream)" "no OPENAI_API_KEY"
fi

# ---- Session persistence (after provider tests generate data) ----
header "Session persistence"
run_test "Session recorded in Postgres" test_session_recorded

# ---- Summary ----
echo
echo "============================================"
printf 'Results: \033[32m%d passed\033[0m' "$PASS"
[ "$FAIL" -gt 0 ] && printf ', \033[31m%d failed\033[0m' "$FAIL"
[ "$SKIP" -gt 0 ] && printf ', \033[33m%d skipped\033[0m' "$SKIP"
echo
echo "Log dir: ${LOGDIR}"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
  echo
  echo "Inspect debug logs:"
  echo "  ls ${LOGDIR}/"
  echo
  echo "Fix failures before switching ANTHROPIC_BASE_URL in settings.json."
  exit 1
else
  echo
  echo "All tests passed. Safe to update settings.json:"
  echo '  "ANTHROPIC_BASE_URL": "'${PROXY_URL}'"'
  exit 0
fi
