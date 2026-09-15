# CLAUDE.md — loom-oss

Project-specific instructions for Claude Code (or any agent) working in this
repository.

## Dashboard changes: standard workflow

Whenever a change touches `dashboard/src/**`, do the following automatically
— do not stop to ask before any of these steps:

1. **Isolate the work.** Use a dedicated git worktree/branch rather than
   editing the shared `main` checkout directly, e.g.:
   ```
   git worktree add ../.loom-oss-worktrees/<name> -b fix/<slug>
   ```

2. **Rebuild the bundle in the same commit.** The gateway serves the
   dashboard as static files baked into the Python package
   (`src/loom/dashboard/static/`) — there is no separate frontend deploy
   step or CI build. After editing anything under `dashboard/src/`, run:
   ```
   cd dashboard && npm run build
   ```
   and commit the rebuilt `src/loom/dashboard/static/**` output alongside
   the source change, in the *same* commit. This repo's history follows
   that convention (e.g. `1f6e9f2`, `1cd0e0a`) — a source-only commit
   leaves the running dashboard stale even after a redeploy.

3. **Push and open a PR automatically.** Push the branch and open a draft
   PR (`gh pr create --draft`). No need to ask first — this is expected
   default behavior for any dashboard or backend change.

4. **Merging to `main` is a human step.** Claude Code does not merge or
   push to `main`/`master` itself under any circumstance, including when
   explicitly asked — this is a hard operating limit, not a preference.
   Merge the PR yourself: GitHub UI, or `gh pr merge <n> --squash
   --delete-branch`.

5. **Once `main` has the merge, redeploy automatically** — Claude Code
   should do this part unprompted as soon as it notices `main` moved,
   without waiting to be asked again:
   ```
   cd ~/Code/loom-oss
   git checkout main && git pull
   docker compose -f docker-compose.homelab.yml build loom
   docker compose -f docker-compose.homelab.yml up -d loom
   curl -s localhost:4444/health | jq .status
   ```
   Then verify both networks survived the recreate — a rebuild has
   silently dropped the external `n8n_n8n-network` attachment before
   (see the 2026-09-07 incident in `docs/homelab-deploy.md`), which
   surfaces later as gateway-key auth failures rather than a failed
   health check:
   ```
   docker inspect loom-oss-loom-1 --format '{{json .NetworkSettings.Networks}}' | jq 'keys'
   # expect: ["loom-oss_default", "n8n_n8n-network"]
   ```
   If `n8n_n8n-network` is missing:
   ```
   docker network connect n8n_n8n-network loom-oss-loom-1 && docker restart loom-oss-loom-1
   ```

Full deploy details, gitignored config files (`loom.homelab.yaml`,
`.env.homelab`, `docker-compose.homelab.yml`), and past incidents are in
`docs/homelab-deploy.md` — read it before touching deploy config.

## Why step 4 is non-negotiable

Merging is the one point in this workflow where a broken change reaches the
production container automatically on the next redeploy. Keeping it a
manual, reviewed action is the actual safeguard; everything else in this
workflow (branching, rebuilding, pushing, opening the PR, redeploying after
merge, verifying networks) is safe to fully automate because it's either
inspectable before merge or reversible after.
