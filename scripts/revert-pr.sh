#!/usr/bin/env bash
# Revert a merged PR on main, with a pre/post snapshot log and optional
# redeploy. Built after the 2026-09-07 incident where PR #87 was reverted,
# then reinstated once its diff was cleared — doing that dance by hand
# twice in one session is what this replaces.
#
# Usage:
#   scripts/revert-pr.sh <PR_NUMBER>              # revert locally only
#   scripts/revert-pr.sh <PR_NUMBER> --push        # revert + push to origin
#   scripts/revert-pr.sh <PR_NUMBER> --push --redeploy
#
# Safe by default: without --push, nothing leaves your machine. Without
# --redeploy, the running container is untouched. Each stage is logged to
# .deploy-log.jsonl (gitignored, local only) so you can see exactly what
# state you were in before and after.
#
# Note for Claude Code sessions: `git revert` on main of this repo is
# treated as a history-altering production action and may be blocked by
# the auto-mode safety classifier even when invoked through this script.
# That's intentional — it forces a human to run this one, not a bug to
# route around.

set -euo pipefail

PR_NUMBER="${1:-}"
PUSH=false
REDEPLOY=false
for arg in "${@:2}"; do
  case "$arg" in
    --push) PUSH=true ;;
    --redeploy) REDEPLOY=true ;;
    *) echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

if [[ -z "$PR_NUMBER" ]]; then
  echo "Usage: $0 <PR_NUMBER> [--push] [--redeploy]" >&2
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
LOG_FILE="$REPO_ROOT/.deploy-log.jsonl"

log() {
  # log <action> <extra_json_fields...>
  local action="$1"; shift
  local extra="${1:-}"
  local ts head
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  head="$(git rev-parse HEAD)"
  if [[ -n "$extra" ]]; then
    printf '{"ts":"%s","action":"%s","head":"%s",%s}\n' "$ts" "$action" "$head" "$extra" >> "$LOG_FILE"
  else
    printf '{"ts":"%s","action":"%s","head":"%s"}\n' "$ts" "$action" "$head" >> "$LOG_FILE"
  fi
}

branch="$(git branch --show-current)"
if [[ "$branch" != "main" ]]; then
  echo "Refusing: current branch is '$branch', expected 'main'." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Refusing: working tree has uncommitted changes to tracked files." >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

echo "==> Looking up PR #$PR_NUMBER"
pr_json="$(gh pr view "$PR_NUMBER" --json number,title,mergeCommit,state)"
pr_title="$(echo "$pr_json" | jq -r .title)"
merge_sha="$(echo "$pr_json" | jq -r .mergeCommit.oid)"
pr_state="$(echo "$pr_json" | jq -r .state)"

if [[ "$pr_state" != "MERGED" || "$merge_sha" == "null" ]]; then
  echo "Refusing: PR #$PR_NUMBER is not in a merged state (state=$pr_state)." >&2
  exit 1
fi

echo "==> PR #$PR_NUMBER: $pr_title"
echo "==> Merge/target commit: $merge_sha"

parents="$(git cat-file -p "$merge_sha" | grep -c '^parent' || true)"

net_state="null"
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -q '^loom-oss-loom-1$'; then
  net_state="$(docker inspect loom-oss-loom-1 --format '{{json .NetworkSettings.Networks}}' | jq -c 'keys')"
fi
log "pre_revert" "\"pr\":$PR_NUMBER,\"pr_title\":$(jq -Rn --arg s "$pr_title" '$s'),\"merge_sha\":\"$merge_sha\",\"networks_before\":$net_state"

echo "==> Reverting (this is the step that may require your explicit approval)"
if [[ "$parents" -ge 2 ]]; then
  git revert --no-edit -m 1 "$merge_sha"
else
  git revert --no-edit "$merge_sha"
fi

new_head="$(git rev-parse HEAD)"
log "reverted" "\"pr\":$PR_NUMBER,\"new_head\":\"$new_head\""
echo "==> Reverted. To undo this specific revert later, run:"
echo "    git revert --no-edit $new_head"

if [[ "$PUSH" == true ]]; then
  echo "==> Pushing to origin/main"
  git push origin main
  log "pushed" "\"pr\":$PR_NUMBER"
fi

if [[ "$REDEPLOY" == true ]]; then
  echo "==> Rebuilding and redeploying"
  docker compose -f docker-compose.homelab.yml up -d --build
  sleep 3
  networks_after="$(docker inspect loom-oss-loom-1 --format '{{json .NetworkSettings.Networks}}' | jq -c 'keys')"
  has_n8n="$(echo "$networks_after" | jq 'any(. == "n8n_n8n-network")')"
  health="$(curl -s localhost:4444/health | jq -r .status 2>/dev/null || echo "unreachable")"
  log "redeploy" "\"pr\":$PR_NUMBER,\"networks_after\":$networks_after,\"health\":\"$health\""
  echo "==> Networks after redeploy: $networks_after"
  echo "==> Health: $health"
  if [[ "$has_n8n" != "true" ]]; then
    echo "!! n8n_n8n-network missing after redeploy — reconnecting (see docs/homelab-deploy.md)" >&2
    docker network connect n8n_n8n-network loom-oss-loom-1
    docker restart loom-oss-loom-1
    log "network_reconnected" "\"pr\":$PR_NUMBER"
  fi
fi

echo "==> Done. Log: $LOG_FILE"
