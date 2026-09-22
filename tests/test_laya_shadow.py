"""laya shadow-mode client: opt-in, off the critical path, comparison-only.

The client talks to a standalone laya sidecar service over HTTP (see
services/laya-sidecar/) — laya itself is never imported in this process,
so these tests mock the HTTP layer, not laya. Mirrors the conventions in
test_llm_prose.py for another optional, model-backed feature that's off by
default and must degrade safely.
"""

import json
from unittest import mock

from loom.config import LayaShadowConfig, LoomConfig
from loom.detection.laya_shadow import LayaShadowClient


def _fake_response(json_data=None, status_code=200):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = RuntimeError(f"http {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


GOOD_RESULT = {
    "answers": {
        "tier": {"choice": "premium", "probabilities": {"economy": 0.05, "standard": 0.15, "premium": 0.80}},
        "needs_reasoning": {"noul": 0.9},
    }
}


def _client(**kwargs):
    client = LayaShadowClient(**kwargs)
    client._client = mock.Mock()  # never touch the network in tests
    return client


def test_config_defaults_are_off():
    cfg = LoomConfig()
    assert cfg.laya_shadow.enabled is False
    assert cfg.laya_shadow.url == "http://localhost:8091"
    assert cfg.laya_shadow.checkpoint == "base"
    assert cfg.laya_shadow.sample_rate == 1.0
    assert cfg.laya_shadow.cache_size == 512


def test_run_writes_comparison_record(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path))
    client._client.post.return_value = _fake_response(GOOD_RESULT)

    client._run("req-1", "test-source", "design a system", "standard", 55.0)

    client._client.post.assert_called_once()
    args, kwargs = client._client.post.call_args
    assert args[0] == "http://localhost:8091/classify"
    assert kwargs["json"]["state"] == {"request": "design a system"}
    assert kwargs["json"]["checkpoint"] == "base"
    assert "tier" in kwargs["json"]["questions"] and "needs_reasoning" in kwargs["json"]["questions"]

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
    assert record["cached"] is False
    assert "prompt_hash" in record


def test_run_agree_true_when_tiers_match(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path))
    client._client.post.return_value = _fake_response(GOOD_RESULT)  # laya says "premium"

    client._run("req-2", "src", "prompt", "premium", 90.0)

    record = json.loads(log_path.read_text().strip())
    assert record["agree"] is True


def test_sidecar_unreachable_is_safe_and_unlogged(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path))
    client._client.post.side_effect = ConnectionError("no route to host")

    client._run("req-3", "src", "prompt", "economy", 10.0)
    client._run("req-4", "src", "prompt", "economy", 10.0)

    # No retry logic to break — every call is independent and silent.
    assert client._client.post.call_count == 2
    assert not log_path.exists()


def test_sidecar_error_status_is_safe_and_unlogged(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path))
    client._client.post.return_value = _fake_response(status_code=500)

    client._run("req-6", "src", "prompt", "economy", 10.0)

    assert not log_path.exists()


def test_malformed_response_is_safe_and_unlogged(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path))
    client._client.post.return_value = _fake_response({"unexpected": "shape"})

    client._run("req-6b", "src", "prompt", "economy", 10.0)

    assert not log_path.exists()


def test_sample_rate_zero_skips_entirely(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path), sample_rate=0.0)
    client._client.post.return_value = _fake_response(GOOD_RESULT)

    client.shadow("req-7", "src", "prompt", "economy", 10.0)
    client._executor.shutdown(wait=True)

    client._client.post.assert_not_called()
    assert not log_path.exists()


def test_long_prompt_is_skipped(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path), max_prompt_chars=10)
    client._client.post.return_value = _fake_response(GOOD_RESULT)

    client.shadow("req-9", "src", "this prompt is way over the limit", "economy", 10.0)
    client._executor.shutdown(wait=True)

    client._client.post.assert_not_called()
    assert not log_path.exists()


def test_cache_hit_avoids_second_call(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path))
    client._client.post.return_value = _fake_response(GOOD_RESULT)

    client._run("req-10", "src", "same prompt", "economy", 10.0)
    client._run("req-11", "src", "same prompt", "economy", 10.0)

    client._client.post.assert_called_once()
    lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["cached"] is False
    assert lines[1]["cached"] is True
    assert lines[1]["laya_tier"] == "premium"


def test_unsafe_url_is_rejected_without_a_call(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    # Link-local (incl. cloud metadata) is never allowed, even with the
    # private opt-in — see netcheck.validate_outbound_url.
    client = _client(
        log_path=str(log_path), url="http://169.254.169.254:80", allow_private_url=True
    )
    client._client.post.return_value = _fake_response(GOOD_RESULT)

    client._run("req-12", "src", "prompt", "economy", 10.0)

    client._client.post.assert_not_called()
    assert not log_path.exists()


def test_shadow_is_fire_and_forget_via_executor(tmp_path):
    log_path = tmp_path / "laya_shadow.jsonl"
    client = _client(log_path=str(log_path))
    client._client.post.return_value = _fake_response(GOOD_RESULT)

    client.shadow("req-8", "src", "hello", "economy", 88.0)
    # Deterministic wait for the background worker instead of a sleep.
    client._executor.shutdown(wait=True)

    record = json.loads(log_path.read_text().strip())
    assert record["request_id"] == "req-8"


def test_close_does_not_raise(tmp_path):
    client = _client(log_path=str(tmp_path / "x.jsonl"))
    client.close()  # must not raise even with no work scheduled


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
