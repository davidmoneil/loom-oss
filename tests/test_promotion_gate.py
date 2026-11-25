import time

import pytest

from loom.observability import promotion_gate as pg
from loom.observability import tool_actions as ta
from loom.storage.sqlite import LoomStorage

DAY = 86400
NOW = 1_000_000_000.0


def _rules(**shadow):
    base = [{"id": "vcs-read", "label": "vcs.read", "status": "active", "match": {"argv0": ["git"], "sub": ["status"]}}]
    base.append({"id": "s1", "label": "vcs.tag", "status": "shadow", "created_at": NOW - shadow.get("age", 8) * DAY,
                 "match": {"argv0": ["git"], "sub": ["tag"]}})
    return base


def _stats(hits=25, overlaps=0):
    return {"s1": {"hits": hits, "overlaps": overlaps, "first_seen": NOW - 8 * DAY, "last_seen": NOW}}


def _ids(items):
    return [i["id"] for i in items]


def test_promote():
    r = pg.evaluate(_rules(), _stats(), NOW)
    assert _ids(r["promoted"]) == ["s1"]
    assert [x["status"] for x in r["rules"]] == ["active", "active"]


def test_too_young_or_few_hits_stays_shadow():
    assert pg.evaluate(_rules(age=3), _stats(), NOW)["kept"] == ["s1"]
    assert pg.evaluate(_rules(), _stats(hits=19), NOW)["kept"] == ["s1"]


def test_reject_overlap_then_expire():
    assert pg.evaluate(_rules(), _stats(overlaps=1), NOW)["kept"] == ["s1"]
    r = pg.evaluate(_rules(age=22), _stats(overlaps=1), NOW)
    assert _ids(r["dropped"]) == ["s1"] and "overlap" in r["dropped"][0]["reason"]
    assert [x["id"] for x in r["rules"]] == ["vcs-read"]


def test_reject_taxonomy_raises_decision_not_promotion():
    rules = _rules()
    rules[1]["label"] = "brandnew.thing"
    r = pg.evaluate(rules, _stats(), NOW)
    assert r["promoted"] == [] and r["kept"] == ["s1"]
    assert r["decisions"][0]["id"] == "s1"


def test_expire_without_meeting_threshold():
    r = pg.evaluate(_rules(age=21), _stats(hits=3), NOW)
    assert _ids(r["dropped"]) == ["s1"]


def test_shadow_matches_never_change_label():
    rules = ta._build(_rules())
    assert ta.classify("Bash", {"command": "git tag v1"}, rules=rules)[0] == "bash.other"
    assert ta.shadow_matches("Bash", {"command": "git tag v1"}, rules=rules) == [("s1", False)]
    assert ta.shadow_matches("Bash", {"command": "git status"}, rules=rules) == []
    assert ta.shadow_matches("Read", {}, rules=rules) == []


def test_shadow_overlap_flag():
    rules = _rules()
    rules.append({"id": "s2", "label": "vcs.read2", "status": "shadow", "match": {"argv0": ["git"], "sub": ["status"]}})
    assert ta.shadow_matches("Bash", {"command": "git status"}, rules=ta._build(rules)) == [("s2", True)]


@pytest.fixture
def store(tmp_path):
    s = LoomStorage(str(tmp_path / "t.db"))
    s.connect()
    s.migrate()
    yield s
    s.close()


def test_run_gate_writes_revision_with_actor(store):
    rules = _rules(age=0)
    rules[1].pop("created_at")
    store.put_classification_rules(rules, "seed")
    store.record_shadow_hits([("s1", False)] * 25)
    # age comes from first_seen: not old enough yet
    assert pg.run_gate(store)["changed"] is False
    res = pg.run_gate(store, now=time.time() + 8 * DAY)
    assert res["changed"] and _ids(res["promoted"]) == ["s1"]
    latest = store.get_classification_rules()
    assert latest["actor"] == "promotion-gate" and latest["revision"] == 2
    assert latest["rules"][1]["status"] == "active"
    ta.set_active_rules(None)


def test_run_gate_decision_callback(store):
    rules = _rules()
    rules[1]["label"] = "brandnew.thing"
    rules[1].pop("created_at")
    store.put_classification_rules(rules, "seed")
    store.record_shadow_hits([("s1", False)] * 25)
    seen = []
    res = pg.run_gate(store, now=time.time() + 8 * DAY, raise_decision=seen.append)
    assert not res["changed"] and seen and seen[0]["id"] == "s1"
