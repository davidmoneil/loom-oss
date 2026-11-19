import pytest

from loom.observability import tool_actions
from loom.observability.tool_actions import classify, load_rules, set_active_rules, validate_rules
from loom.storage import LoomStorage


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_active_rules(None)


@pytest.fixture
def store(tmp_path):
    st = LoomStorage(db_path=str(tmp_path / "c.db"))
    st.connect()
    st.migrate()
    return st


def test_default_rules_valid():
    assert validate_rules(load_rules()) == []


@pytest.mark.parametrize("bad", [
    "x", [1], [{"label": "a.b", "match": {"argv0": ["x"]}}],
    [{"id": "a", "label": "a.b", "match": {"argv0": ["x"]}}, {"id": "a", "label": "a.c", "match": {"argv0": ["y"]}}],
    [{"id": "a", "label": "a.b", "status": "bogus", "match": {"argv0": ["x"]}}],
    [{"id": "a", "label": "a.b", "match": {}}],
])
def test_validate_rejects(bad):
    assert validate_rules(bad)


def test_revisions_append(store):
    assert store.get_classification_rules() is None
    r1 = store.put_classification_rules(load_rules(), "seed")
    r2 = store.put_classification_rules([], "tester")
    assert (r1["revision"], r2["revision"]) == (1, 2)
    latest = store.get_classification_rules()
    assert latest["revision"] == 2 and latest["actor"] == "tester" and latest["rules"] == []


def test_set_active_rules_hot_reload():
    assert classify("Bash", {"command": "zzz run"})[0] == "bash.other"
    set_active_rules([{"id": "z", "label": "custom.z", "status": "active", "match": {"argv0": ["zzz"]}}])
    assert classify("Bash", {"command": "zzz run"})[0] == "custom.z"
    set_active_rules(None)
    assert classify("Bash", {"command": "zzz run"})[0] == "bash.other"


def test_signatures_ordered(store):
    store.record_action_signatures([("Bash", "bash.other", "foo"), ("Bash", "bash.other", "bar"), ("Bash", "bash.other", "bar")])
    sigs = store.get_action_signatures()
    assert [s["signature"] for s in sigs] == ["bar", "foo"]


def test_api_roundtrip(store):
    from fastapi.testclient import TestClient

    from loom.gateway.app import _load_classification_rules, app
    gw = app.state.gateway
    old = gw.storage
    gw.storage = store
    try:
        _load_classification_rules(store)
        c = TestClient(app)
        r = c.get("/api/v1/classification/rules")
        assert r.status_code == 200 and r.json()["revision"] == 1
        assert c.put("/api/v1/classification/rules", json={"rules": "no"}).status_code == 422
        rule = {"id": "z", "label": "custom.z", "match": {"argv0": ["zzz"]}}
        r = c.put("/api/v1/classification/rules", json={"rules": [rule], "actor": "t"})
        assert r.status_code == 200 and r.json()["revision"] == 2
        assert classify("Bash", {"command": "zzz"})[0] == "custom.z"
        assert c.get("/api/v1/classification/signatures").status_code == 200
    finally:
        gw.storage = old
