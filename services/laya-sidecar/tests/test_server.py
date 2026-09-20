"""Tests for the laya sidecar HTTP service.

Mocks laya.load / agent.predict throughout — real laya weights (~2.4GB per
resident checkpoint) must never be a test dependency.
"""

from __future__ import annotations

import os
import sys
import time
import types
from unittest import mock

import pytest

# Preloading both checkpoints at import time would try `import laya` for
# real; tests load checkpoints lazily and mock laya per-test instead.
os.environ.setdefault("LAYA_SIDECAR_PRELOAD", "0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _fake_laya_module(predict_result=None, predict_side_effect=None, load_side_effect=None):
    """Builds a fake ``laya`` module exposing ``laya.load(...) -> agent``."""
    agent = mock.Mock()
    if predict_side_effect is not None:
        agent.predict.side_effect = predict_side_effect
    else:
        agent.predict.return_value = predict_result
    module = types.ModuleType("laya")
    if load_side_effect is not None:
        module.load = mock.Mock(side_effect=load_side_effect)
    else:
        module.load = mock.Mock(return_value=agent)
    return module, agent


GOOD_RESULT = {
    "answers": {
        "tier": {"choice": "premium", "probabilities": {"economy": 0.05, "standard": 0.15, "premium": 0.8}},
    }
}


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test gets a clean slate of loaded checkpoints/errors."""
    server_module._agents.clear()
    server_module._load_errors.clear()
    yield
    server_module._agents.clear()
    server_module._load_errors.clear()


@pytest.fixture
def client():
    with TestClient(server_module.app) as c:
        yield c


def test_health_before_any_load(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "starting"
    assert body["checkpoints_loaded"] == []
    assert body["checkpoints_available"] == ["base", "typed-decisions"]


def test_classify_loads_checkpoint_and_returns_answers(client):
    fake_module, agent = _fake_laya_module(predict_result=GOOD_RESULT)
    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        resp = client.post(
            "/classify",
            json={"state": {"request": "hi"}, "questions": {"tier": {}}, "checkpoint": "base"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answers"] == GOOD_RESULT["answers"]
    assert body["checkpoint"] == "base"
    assert "latency_ms" in body
    agent.predict.assert_called_once_with({"request": "hi"}, {"tier": {}})
    fake_module.load.assert_called_once_with("convaiinnovations/laya", device="cpu", subfolder=None)


def test_classify_reuses_loaded_checkpoint(client):
    fake_module, agent = _fake_laya_module(predict_result=GOOD_RESULT)
    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        client.post("/classify", json={"state": {"a": 1}, "questions": {"q": {}}})
        client.post("/classify", json={"state": {"a": 2}, "questions": {"q": {}}})
    assert fake_module.load.call_count == 1
    assert agent.predict.call_count == 2


def test_classify_unknown_checkpoint_is_400(client):
    resp = client.post(
        "/classify",
        json={"state": {"a": 1}, "questions": {"q": {}}, "checkpoint": "nonexistent"},
    )
    assert resp.status_code == 400


def test_classify_empty_state_is_400(client):
    resp = client.post("/classify", json={"state": {}, "questions": {"q": {}}})
    assert resp.status_code == 400


def test_classify_empty_questions_is_400(client):
    resp = client.post("/classify", json={"state": {"a": 1}, "questions": {}})
    assert resp.status_code == 400


def test_classify_checkpoint_load_failure_is_503(client):
    fake_module, _ = _fake_laya_module(load_side_effect=RuntimeError("no weights"))
    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        resp = client.post("/classify", json={"state": {"a": 1}, "questions": {"q": {}}})
    assert resp.status_code == 503


def test_classify_predict_failure_is_500(client):
    fake_module, _ = _fake_laya_module(predict_side_effect=RuntimeError("boom"))
    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        resp = client.post("/classify", json={"state": {"a": 1}, "questions": {"q": {}}})
    assert resp.status_code == 500


def test_classify_timeout_is_504(client, monkeypatch):
    monkeypatch.setattr(server_module, "REQUEST_TIMEOUT_SECONDS", 0.05)

    def _slow_predict(state, questions):
        time.sleep(0.3)
        return GOOD_RESULT

    fake_module, _ = _fake_laya_module(predict_side_effect=_slow_predict)
    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        resp = client.post("/classify", json={"state": {"a": 1}, "questions": {"q": {}}})
    assert resp.status_code == 504


def test_classify_typed_decisions_checkpoint_uses_subfolder(client):
    fake_module, agent = _fake_laya_module(predict_result=GOOD_RESULT)
    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        resp = client.post(
            "/classify",
            json={"state": {"a": 1}, "questions": {"q": {}}, "checkpoint": "typed-decisions"},
        )
    assert resp.status_code == 200
    fake_module.load.assert_called_once_with(
        "convaiinnovations/laya", device="cpu", subfolder="typed-decisions"
    )


def test_health_reports_loaded_checkpoint(client):
    fake_module, _ = _fake_laya_module(predict_result=GOOD_RESULT)
    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        client.post("/classify", json={"state": {"a": 1}, "questions": {"q": {}}})
    resp = client.get("/health")
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["checkpoints_loaded"] == ["base"]


def test_health_reports_load_error(client):
    fake_module, _ = _fake_laya_module(load_side_effect=RuntimeError("no weights"))
    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        client.post("/classify", json={"state": {"a": 1}, "questions": {"q": {}}})
    resp = client.get("/health")
    body = resp.json()
    assert body["status"] == "degraded"
    assert "base" in body["checkpoint_errors"]
