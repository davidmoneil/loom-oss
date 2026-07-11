#!/usr/bin/env python3
"""Compression parity harness: loom-oss vs the internal proxy.

Fires identical payloads at two gateways and compares compression using
each gateway's cumulative /health counters (tokens_before / tokens_after),
sampled around every request. Compression happens before the upstream
call, so an invalid API key still exercises the full compression path —
the harness costs zero API tokens.

Usage:
    python tests/parity_harness.py \
        --internal http://localhost:8711 \
        --oss http://localhost:4555 \
        [--tier medium] [--json results.json]

The parity criterion (AIProjects-878r): OSS aggregate tokens_saved within
5% of internal on the sample.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Payload generation — deterministic, three representative categories
# ---------------------------------------------------------------------------

_WORDS = (
    "deployment pipeline container registry rollback canary healthcheck "
    "latency throughput retry backoff timeout quota shard replica leader "
    "election consensus snapshot compaction index query planner cache "
    "eviction warmup coldstart migration schema constraint transaction"
).split()

_FILLER_SENTENCES = (
    "So basically what happened is that the {} process, you know, actually "
    "completed successfully and everything worked fine in the end. ",
    "I think we should probably take a look at the {} configuration because "
    "it seems like it might be related to the issue we saw earlier today. ",
    "Just to summarize what we discussed, the {} needs to be updated before "
    "we can move forward with the rest of the plan, generally speaking. ",
)


def _prose(rng: random.Random, sentences: int) -> str:
    return "".join(
        rng.choice(_FILLER_SENTENCES).format(rng.choice(_WORDS))
        for _ in range(sentences)
    )


def _log_blob(rng: random.Random, lines: int) -> str:
    return "\n".join(
        f"2026-07-11 07:{i % 60:02d}:{(i * 7) % 60:02d} INFO worker-{i % 4} "
        f"processed batch {i} of {rng.choice(_WORDS)} in {rng.randint(2, 900)}ms "
        f"status={'ok' if i % 9 else 'retry'}"
        for i in range(lines)
    )


def _conversation_payload(rng: random.Random, turns: int) -> list[dict]:
    msgs = []
    for i in range(turns):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"turn {i}: " + _prose(rng, 14)})
    return msgs


def _tool_heavy_payload(rng: random.Random, rounds: int) -> list[dict]:
    msgs = [{"role": "user", "content": "Investigate the failing service. " + _prose(rng, 4)}]
    for i in range(rounds):
        msgs.append({
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking the next log segment. " + _prose(rng, 2)},
                {"type": "tool_use", "id": f"toolu_{i}", "name": "bash",
                 "input": {"cmd": f"kubectl logs deploy/svc --since={i}h"}},
            ],
        })
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{i}",
                 "content": _log_blob(rng, 80)},
            ],
        })
    msgs.append({"role": "user", "content": "What did you find?"})
    return msgs


def _mixed_payload(rng: random.Random, rounds: int) -> list[dict]:
    msgs = _conversation_payload(rng, 6)
    for i in range(rounds):
        msgs.append({
            "role": "assistant",
            "content": [
                {"type": "text", "text": _prose(rng, 6)},
                {"type": "tool_use", "id": f"toolu_m{i}", "name": "read_file",
                 "input": {"path": f"/src/module_{i}.py"}},
            ],
        })
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_m{i}",
                 "content": _log_blob(rng, 40) + "\n" + _prose(rng, 8)},
            ],
        })
        msgs.append({"role": "assistant", "content": _prose(rng, 10)})
    msgs.append({"role": "user", "content": "Summarize the findings."})
    return msgs


def build_payloads() -> list[tuple[str, list[dict]]]:
    rng = random.Random(878)  # task id as seed — reproducible sample
    payloads: list[tuple[str, list[dict]]] = []
    for i in range(5):
        payloads.append((f"conversation-{i}", _conversation_payload(rng, 24 + 4 * i)))
    for i in range(5):
        payloads.append((f"tool-heavy-{i}", _tool_heavy_payload(rng, 6 + 2 * i)))
    for i in range(5):
        payloads.append((f"mixed-{i}", _mixed_payload(rng, 4 + i)))
    return payloads


# ---------------------------------------------------------------------------
# Gateway I/O
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def health_compression(base: str) -> tuple[int, int]:
    comp = _get_json(f"{base}/health").get("compression", {})
    return int(comp.get("tokens_before", 0)), int(comp.get("tokens_after", 0))


def fire(base: str, messages: list[dict], model: str, tier: str) -> int:
    """POST /v1/messages with a bogus key; returns HTTP status.

    Upstream auth failures (401) are expected and fine — compression has
    already run and been counted by the time the gateway forwards.
    """
    body = json.dumps({
        "model": model,
        "max_tokens": 16,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        f"{base}/v1/messages",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": "sk-parity-harness-not-a-real-key",
            "authorization": "Bearer sk-parity-harness-not-a-real-key",
            "anthropic-version": "2023-06-01",
            "x-loom-source": "parity-harness",
            "x-loom-compression": tier,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def measure(base: str, name: str, messages: list[dict], model: str, tier: str) -> dict:
    b0, a0 = health_compression(base)
    status = fire(base, copy.deepcopy(messages), model, tier)
    time.sleep(0.3)  # let async counters settle
    b1, a1 = health_compression(base)
    before, after = b1 - b0, a1 - a0
    return {
        "payload": name,
        "status": status,
        "tokens_before": before,
        "tokens_after": after,
        "tokens_saved": max(0, before - after),
        "ratio": round(1 - after / before, 4) if before > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--internal", default="http://localhost:8711")
    ap.add_argument("--oss", default="http://localhost:4555")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--tier", default="medium")
    ap.add_argument("--threshold", type=float, default=0.05)
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    for label, base in (("internal", args.internal), ("oss", args.oss)):
        try:
            _get_json(f"{base}/health")
        except Exception as exc:
            print(f"FATAL: {label} gateway unreachable at {base}: {exc}")
            return 2

    payloads = build_payloads()
    rows = []
    print(f"{'payload':<18} {'internal saved':>14} {'ratio':>7} "
          f"{'oss saved':>10} {'ratio':>7} {'delta':>8}")
    for name, messages in payloads:
        internal = measure(args.internal, name, messages, args.model, args.tier)
        oss = measure(args.oss, name, messages, args.model, args.tier)
        delta = (
            (oss["tokens_saved"] - internal["tokens_saved"])
            / internal["tokens_saved"]
            if internal["tokens_saved"] > 0 else 0.0
        )
        rows.append({"payload": name, "internal": internal, "oss": oss,
                     "delta": round(delta, 4)})
        print(f"{name:<18} {internal['tokens_saved']:>14} "
              f"{internal['ratio']:>7.1%} {oss['tokens_saved']:>10} "
              f"{oss['ratio']:>7.1%} {delta:>8.1%}")

    int_total = sum(r["internal"]["tokens_saved"] for r in rows)
    oss_total = sum(r["oss"]["tokens_saved"] for r in rows)
    agg_delta = (oss_total - int_total) / int_total if int_total else 0.0
    verdict = "PASS" if abs(agg_delta) <= args.threshold or agg_delta > 0 else "FAIL"
    print(f"\naggregate tokens_saved: internal={int_total} oss={oss_total} "
          f"delta={agg_delta:+.1%} (threshold ±{args.threshold:.0%}) → {verdict}")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({
                "tier": args.tier,
                "rows": rows,
                "aggregate": {
                    "internal_tokens_saved": int_total,
                    "oss_tokens_saved": oss_total,
                    "delta": round(agg_delta, 4),
                    "verdict": verdict,
                },
            }, fh, indent=2)
        print(f"results written to {args.json_out}")

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
