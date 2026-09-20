"""laya shadow-mode classifier: opt-in, off the critical path, comparison-only.

Mirrors the conventions in test_llm_prose.py for another optional,
model-backed feature that's off by default and must degrade safely.
"""

import json
import sys
import types
from unittest import mock

import pytest

from loom.config import LayaShadowConfig, LoomConfig
from loom.detection.laya_shadow import LayaShadowRunner


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
        "tier": {"choice": "premium", "probabilities": {"economy": 0.05, "standard": 0.15, "premium": 0.80}},
        "needs_reasoning": {"noul": 0.9},
    }
}


def test_config_defaults_are_off():
    cfg = LoomConfig()
    assert cfg.laya_shadow.enabled is False
    assert cfg.laya_shadow.model_id == "convaiinnovations/laya"
    assert cfg.laya_shadow.sample_rate == 1.0


def test_run_writes_comparison_record(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    runner = LayaShadowRunner(log_path=str(log_path))
    fake_module, agent = _fake_laya_module(predict_result=GOOD_RESULT)

    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        runner._run("req-1", "test-source", "design a system", "standard", 55.0)

    agent.predict.assert_called_once()
    (state, questions), _ = agent.predict.call_args
    assert state == {"request": "design a system"}
    assert "tier" in questions and "needs_reasoning" in questions

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["request_id"] == "req-1"
    assert record["source"] == "test-source"
    assert record["rule_tier"] == "standard"
    assert record["rule_confidence"] == 55.0
    assert record["laya_tier"] == "premium"
    assert record["laya_probabilities"] == GOOD_RESULT["answers"]["tier"]["probabilities"]
    assert record["laya_needs_reasoning"] == 0.9
    assert record["agree"] is False
    assert "laya_latency_ms" in record


def test_run_agree_true_when_tiers_match(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    runner = LayaShadowRunner(log_path=str(log_path))
    fake_module, _ = _fake_laya_module(predict_result=GOOD_RESULT)  # laya says "premium"

    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        runner._run("req-2", "src", "prompt", "premium", 90.0)

    record = json.loads(log_path.read_text().strip())
    assert record["agree"] is True


def test_model_load_failure_is_sticky_and_safe(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    runner = LayaShadowRunner(log_path=str(log_path))
    fake_module, _ = _fake_laya_module(load_side_effect=RuntimeError("no weights"))

    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        runner._run("req-3", "src", "prompt", "economy", 10.0)
        runner._run("req-4", "src", "prompt", "economy", 10.0)

    # load() attempted only once — failure is sticky, no retry storm.
    assert fake_module.load.call_count == 1
    assert not log_path.exists()


def test_missing_laya_package_is_safe(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    runner = LayaShadowRunner(log_path=str(log_path))

    with mock.patch.dict(sys.modules, {"laya": None}):  # forces ImportError
        runner._run("req-5", "src", "prompt", "economy", 10.0)

    assert not log_path.exists()
    assert runner._load_failed is True


def test_predict_failure_is_safe_and_unlogged(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    runner = LayaShadowRunner(log_path=str(log_path))
    fake_module, _ = _fake_laya_module(predict_side_effect=RuntimeError("boom"))

    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        runner._run("req-6", "src", "prompt", "economy", 10.0)

    assert not log_path.exists()


def test_sample_rate_zero_skips_entirely(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    runner = LayaShadowRunner(log_path=str(log_path), sample_rate=0.0)
    fake_module, agent = _fake_laya_module(predict_result=GOOD_RESULT)

    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        runner.shadow("req-7", "src", "prompt", "economy", 10.0)
        runner._executor.shutdown(wait=True)

    agent.predict.assert_not_called()
    assert not log_path.exists()


def test_shadow_is_fire_and_forget_via_executor(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    runner = LayaShadowRunner(log_path=str(log_path))
    fake_module, agent = _fake_laya_module(predict_result=GOOD_RESULT)

    with mock.patch.dict(sys.modules, {"laya": fake_module}):
        runner.shadow("req-8", "src", "hello", "economy", 88.0)
        # Deterministic wait for the background worker instead of a sleep.
        runner._executor.shutdown(wait=True)

    record = json.loads(log_path.read_text().strip())
    assert record["request_id"] == "req-8"


def test_close_does_not_raise(tmp_path):
    runner = LayaShadowRunner(log_path=str(tmp_path / "x.jsonl"))
    runner.close()  # must not raise even with no work scheduled


# --------------------------------------------------------------------------- #
#  _shadow_observe: the hook on the REAL request path
#
#  Regression cover for a gap in the first implementation: the shadow call was
#  wired only into /v1/detect, a diagnostic endpoint that production traffic
#  never reaches, so shadow mode collected nothing from real usage.
# --------------------------------------------------------------------------- #

class _RecordingShadow:
    def __init__(self, explode=False):
        self.calls = []
        self.explode = explode

    def shadow(self, request_id, source, prompt, rule_tier=None, rule_confidence=None):
        if self.explode:
            raise RuntimeError("shadow blew up")
        self.calls.append(
            {"request_id": request_id, "source": source, "prompt": prompt,
             "rule_tier": rule_tier, "rule_confidence": rule_confidence}
        )


class _FakeGateway:
    def __init__(self, shadow=None, detection=None):
        self.laya_shadow = shadow
        self.detection = detection


def _observe(gw, messages, source="s", request_id="r1"):
    from loom.gateway.app import _shadow_observe
    _shadow_observe(gw, request_id, source, messages)


def test_shadow_observe_fires_on_chat_messages():
    sh = _RecordingShadow()
    _observe(_FakeGateway(sh), [{"role": "user", "content": "Prove this is correct."}])
    assert len(sh.calls) == 1
    assert sh.calls[0]["prompt"] == "Prove this is correct."


def test_shadow_observe_includes_rule_tier_when_detection_available():
    from loom.detection.engine import DetectionEngine
    sh = _RecordingShadow()
    _observe(_FakeGateway(sh, DetectionEngine()),
             [{"role": "user", "content": "What's the capital of France?"}])
    assert sh.calls[0]["rule_tier"] in ("economy", "standard", "premium")
    assert sh.calls[0]["rule_confidence"] is not None


def test_shadow_observe_works_without_detection_engine():
    sh = _RecordingShadow()
    _observe(_FakeGateway(sh, None), [{"role": "user", "content": "hello"}])
    assert sh.calls[0]["rule_tier"] is None


def test_shadow_observe_noop_when_disabled():
    _observe(_FakeGateway(None), [{"role": "user", "content": "hi"}])  # must not raise


def test_shadow_observe_swallows_failures():
    """A broken shadow must never surface on the request path."""
    _observe(_FakeGateway(_RecordingShadow(explode=True)),
             [{"role": "user", "content": "hi"}])  # must not raise


def test_shadow_observe_handles_multimodal_content():
    sh = _RecordingShadow()
    _observe(_FakeGateway(sh), [
        {"role": "user", "content": [{"type": "text", "text": "describe this"},
                                     {"type": "image_url", "image_url": {"url": "x"}}]},
    ])
    assert "describe this" in sh.calls[0]["prompt"]


def test_shadow_observe_skips_empty_prompt():
    sh = _RecordingShadow()
    _observe(_FakeGateway(sh), [{"role": "user", "content": ""}])
    assert sh.calls == []


def test_shadow_observe_joins_multiple_messages():
    sh = _RecordingShadow()
    _observe(_FakeGateway(sh), [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "explain gc"},
    ])
    assert "be terse" in sh.calls[0]["prompt"]
    assert "explain gc" in sh.calls[0]["prompt"]
