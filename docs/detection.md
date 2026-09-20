# Detection

`loom.detection` classifies an incoming request to inform routing: which
model tier (`economy` / `standard` / `premium`) is cheapest while still
correct for this prompt.

## Rule-based engine (default, always on)

`DetectionEngine` (`loom.detection.engine`) is a fast, dependency-free
complexity analyzer: it scores a prompt 0-100 from token length, XML tags
that signal structured/high-effort output (`<analysis>`, `<architecture>`,
`<plan>`, ...), tool-call patterns, and the presence of code blocks, then
maps that score to a tier with a confidence value and a human-readable
reason. It has no model dependency and no cost-ledger dependency, so it's
always available and always fast (sub-millisecond).

It's exposed directly at `POST /v1/detect`:

```json
// request
{"source": "my-app", "prompt": "Design a multi-region failover architecture..."}

// response
{
  "request_id": "...",
  "source": "bench",
  "recommended_tier": "premium",
  "confidence": 78.4,
  "reason": "high complexity (82/100); 2 complex XML tags",
  "features": { "token_estimate": 41, "xml_tag_count": 2, "...": "..." }
}
```

## laya shadow mode (optional, off by default)

[laya](https://huggingface.co/convaiinnovations/laya) is an ML prompt
classifier (a calibrated, non-autoregressive model — not an LLM) that can be
run *in shadow* alongside `DetectionEngine` purely to compare tier
predictions. This is an observability/eval feature, not a routing feature:

- It never changes the tier returned by `/v1/detect` — the rule-based
  result is always what callers get back.
- It runs off the request's critical path entirely: model load and
  inference both happen on a dedicated single-worker background thread, so
  a slow load or a slow prediction can never add latency to `/v1/detect`
  (or block the gateway's event loop).
- Every shadow prediction is logged as a JSONL record (`laya_shadow.enabled`
  + `laya_shadow.log_path`) alongside the rule-based result, for offline
  agreement/accuracy analysis.
- Any failure — the optional `laya` package isn't installed, the model
  fails to load, or a single prediction errors — is caught, logged once as
  a warning, and otherwise silent. The load failure is sticky (no retry
  storm on every request).

Off by default. To enable it:

```bash
pip install 'loom-gateway[laya]'   # installs the optional `laya` package
```

```yaml
laya_shadow:
  enabled: true
  model_id: convaiinnovations/laya
  device: cpu
  sample_rate: 1.0              # fraction of /v1/detect calls to also shadow
  log_path: logs/laya_shadow.jsonl
```

`sample_rate` controls cost: laya is a ~421M-parameter model. The model
card's per-call figure is optimistic — measured on this project's own eval
hardware (`laya-eval/bench.py`) it's closer to ~143ms fixed cost plus
~39ms per additional question in the same batch (this integration asks two
questions per call, `tier` and `needs_reasoning`, so budget roughly
180-220ms per shadowed request), versus sub-millisecond for the rule-based
engine. On a busy gateway you likely want `sample_rate` well below `1.0`.

Each log line:

```json
{
  "ts": "2026-09-20T19:32:00+00:00",
  "request_id": "...",
  "source": "bench",
  "rule_tier": "standard",
  "rule_confidence": 55.0,
  "laya_tier": "premium",
  "laya_probabilities": {"economy": 0.05, "standard": 0.15, "premium": 0.80},
  "laya_needs_reasoning": 0.9,
  "agree": false,
  "laya_latency_ms": 34.2
}
```

`gw.laya_shadow is not None` (i.e. whether shadow mode is configured and
was constructed successfully) is surfaced at `GET /health` as
`laya_shadow_enabled`. Note this reflects configuration, not a guarantee
that `laya` is actually installed and loadable — that failure mode is
logged but intentionally doesn't fail health, since it can only ever affect
the shadow log, never a caller's tier.

| Config field | Default | Meaning |
|---|---|---|
| `laya_shadow.enabled` | `false` | Turn shadow mode on |
| `laya_shadow.model_id` | `convaiinnovations/laya` | HF model id passed to `laya.load` |
| `laya_shadow.device` | `cpu` | Inference device |
| `laya_shadow.sample_rate` | `1.0` | Fraction of `/v1/detect` calls to shadow |
| `laya_shadow.log_path` | `logs/laya_shadow.jsonl` | Comparison log path |
