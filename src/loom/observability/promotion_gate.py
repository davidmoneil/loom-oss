"""Daily promotion gate for shadow classification rules.

Shadow rules are evaluated next to active ones (``tool_actions.shadow_matches``)
and never change stored labels. This gate reads their hit/overlap counters and:

* promotes a rule (``status: active``) when age >= 7d, hits >= 20, zero overlap
  with active rules, and its label family already exists in the active taxonomy;
* drops a rule that is >= 21d old and still does not qualify;
* never auto-promotes a brand-new top-level label family: it raises a decision
  (``raise_decision`` callback) and leaves the rule in shadow.

Every change is a new rules revision with ``actor="promotion-gate"``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

PROMOTE_MIN_AGE_S = 7 * 86400
PROMOTE_MIN_HITS = 20
DROP_AGE_S = 21 * 86400
ACTOR = "promotion-gate"

log = logging.getLogger("loom.promotion_gate")


def _family(label: str) -> str:
    return str(label).split(".", 1)[0]


def evaluate(rules: list[dict], stats: dict, now: float) -> dict:
    """Pure decision step. Returns ``{"rules", "promoted", "dropped", "kept", "decisions"}``.

    ``promoted``/``dropped`` entries are ``{"id", "reason"}``; ``decisions`` lists
    shadow rules that need a human call (new top-level label family).
    """
    families = {_family(r.get("label", "")) for r in rules if r.get("status", "active") == "active"}
    out_rules: list[dict] = []
    promoted: list[dict] = []
    dropped: list[dict] = []
    kept: list[str] = []
    decisions: list[dict] = []
    for r in rules:
        if r.get("status", "active") != "shadow":
            out_rules.append(r)
            continue
        rid = str(r.get("id"))
        st = stats.get(rid) or {}
        hits = int(st.get("hits") or 0)
        overlaps = int(st.get("overlaps") or 0)
        started = r.get("created_at") or st.get("first_seen")
        age = (now - float(started)) if started else 0.0
        family_ok = _family(r.get("label", "")) in families
        meets = age >= PROMOTE_MIN_AGE_S and hits >= PROMOTE_MIN_HITS and overlaps == 0
        if meets and family_ok:
            out_rules.append({**r, "status": "active"})
            promoted.append({"id": rid, "reason": f"age>=7d hits={hits} overlap=0"})
        elif age >= DROP_AGE_S:
            if not meets:
                why = "overlap" if overlaps else "insufficient hits" if hits < PROMOTE_MIN_HITS else "new label family"
                dropped.append({"id": rid, "reason": f"expired at 21d: {why} (hits={hits}, overlaps={overlaps})"})
            else:  # qualifies but is a new family and nobody decided: expire
                dropped.append({"id": rid, "reason": "expired at 21d: new label family not approved"})
        else:
            out_rules.append(r)
            kept.append(rid)
            if meets and not family_ok:
                decisions.append({
                    "id": rid,
                    "label": r.get("label"),
                    "reason": f"new top-level label family {_family(r.get('label', ''))!r} needs a human decision",
                })
    return {"rules": out_rules, "promoted": promoted, "dropped": dropped, "kept": kept, "decisions": decisions}


def run_gate(
    storage: Any,
    now: Optional[float] = None,
    raise_decision: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Apply :func:`evaluate` to the stored rules; write a revision when anything changed."""
    now = time.time() if now is None else now
    latest = storage.get_classification_rules()
    if latest is None:
        return {"changed": False, "revision": None, "promoted": [], "dropped": [], "decisions": []}
    result = evaluate(latest["rules"], storage.get_shadow_stats(), now)
    for d in result["decisions"]:
        try:
            (raise_decision or (lambda x: log.warning("decision needed: %s", x)))(d)
        except Exception as exc:  # a failed notification must not block the gate
            log.warning("raise_decision failed: %s", type(exc).__name__)
    changed = bool(result["promoted"] or result["dropped"])
    revision = latest["revision"]
    if changed:
        from loom.observability import tool_actions

        saved = storage.put_classification_rules(result["rules"], ACTOR)
        tool_actions.set_active_rules(result["rules"])
        revision = saved["revision"]
    return {
        "changed": changed,
        "revision": revision,
        "promoted": result["promoted"],
        "dropped": result["dropped"],
        "decisions": result["decisions"],
    }
