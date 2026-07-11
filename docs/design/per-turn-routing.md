# Per-Turn Model Routing

**Status**: Design consideration — not yet implemented
**Created**: 2026-07-04

## Problem

Today, EQRT makes one routing decision per job (headless Nexus tasks). Interactive
Claude Code sessions bypass routing entirely — they flow through the proxy for
compression and observability, but the model is fixed for the session by the
client's `settings.json` (e.g., `claude-opus-4-6`).

Within a single session, turn complexity varies widely:

- Simple tool calls (file reads, `git status`) need minimal reasoning
- Mechanical edits (rename a variable, fix a typo) are low-tier work
- Complex debugging, architecture decisions, and multi-file refactors need
  full-tier reasoning

Routing every turn to the most capable (and most expensive) model wastes budget
on turns where a smaller model would produce identical results.

## What exists today

The infrastructure for per-turn routing is already in place:

1. **Interactive sessions route through the proxy** — every API call from Claude
   Code hits `gateway/app.py` via `ANTHROPIC_BASE_URL`
2. **`_select_model()` runs on every request** — the gateway already classifies
   each request (`_classify_task_type`) and calls the routing engine per-request
3. **EQRT handles task-type + source-policy routing** — the algorithm
   (Eliminate → Qualify → Rank → Tiebreak) already supports task-type-aware
   model selection
4. **The task classifier exists** — currently 4 types in OSS (`general`,
   `code_generation`, `analysis`, `search`), 17 in internal

The gap is not infrastructure — it's policy and data:

- **No interactive source policy enables routing** — interactive sessions
  currently pass `client_specified` as their routing reason, so EQRT never runs
- **The task classifier is too coarse** — 4 types can't distinguish "read a file
  and confirm it exists" from "debug a race condition across 3 services"
- **No empirical data on turn-level quality** — EQRT's quality scores come from
  job-level evaluation; we don't know whether Haiku produces identical results
  to Opus on a simple `git status` turn within an interactive session
- **Context continuity** — unlike independent jobs, turns within a session share
  context; switching models mid-session may affect coherence

## What we'd need before implementing

### Data collection (can start now)

- [ ] Log turn-level metadata for interactive sessions: task type, token count,
  tool calls made, turn duration, model used
- [ ] Tag turns with complexity signals: number of files touched, whether the
  turn involved reasoning vs. mechanical work, user corrections on the
  following turn
- [ ] Build a dataset of turn pairs: (turn metadata) → (quality outcome),
  where quality is measured by whether the user accepted the result without
  correction

### Analysis (needs data)

- [ ] Identify which turn types produce equivalent quality across model tiers
- [ ] Measure context-switch cost: does changing models mid-session degrade
  quality on subsequent turns?
- [ ] Quantify potential savings: what percentage of interactive turns could
  drop to a cheaper tier without quality loss?

### Implementation (needs analysis)

- [ ] Extend the task classifier to produce a complexity score, not just a type
- [ ] Add a `per_turn_routing` flag to source policies (opt-in, default off)
- [ ] Handle the model-switch UX: Claude Code shows the model name — switching
  mid-session may confuse users unless surfaced clearly
- [ ] Define fallback behavior: if the cheaper model fails or produces low
  confidence, retry on the higher tier
- [ ] Session coherence guard: limit how often the model can switch within a
  session (e.g., sticky for N turns after a switch)

## Design constraints

- **Opt-in only** — per-turn routing should never activate unless the source
  policy explicitly enables it. The default interactive experience stays
  single-model.
- **Quality floor** — the routing decision must never reduce quality below what
  the user would get without routing. If in doubt, use the higher tier.
- **Transparency** — the observability API should expose which model handled
  each turn, so users can audit routing decisions.
- **No context fragmentation** — the proxy doesn't own the conversation context
  (Claude Code does). Model switches must not break context continuity. This
  likely means the proxy can only route to models from the same provider
  family.

## Relationship to existing systems

- **EQRT** — per-turn routing would reuse the existing EQRT algorithm, just
  called with finer-grained task types and an interactive source profile
- **Compression** — compression runs regardless of routing; a turn routed to
  Haiku still gets the same compression pipeline
- **Observability** — per-request metrics already record the model used;
  per-turn routing just makes that field vary within a session
- **Governor** — throttle tiers would apply per-model; a session that routes
  some turns to Haiku might have different rate limits for those turns

## Open questions

1. **Provider-lock or cross-provider?** Can we route Turn A to Anthropic Opus
   and Turn B to Ollama Llama, or must all turns stay within one provider for
   context compatibility?
2. **Who decides?** Should the proxy make the routing decision autonomously, or
   should it recommend and let the client (Claude Code) accept/reject?
3. **Granularity** — is per-turn the right level, or should we route per-subtask
   (e.g., a subagent gets a different model than the main loop)?
4. **Interaction with `/fast`** — Claude Code's `/fast` toggle already lets
   users switch models mid-session. Per-turn routing is the automated version
   of that intent. Should they compose or conflict?
