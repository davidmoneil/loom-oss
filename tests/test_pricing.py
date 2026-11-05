"""Cost estimates: price lookup, cache pricing, and the gateway's _model_cost."""

import pytest

from loom import pricing
from loom.config import LoomConfig, ModelConfig
from loom.gateway import app as app_module


@pytest.fixture(autouse=True)
def _no_overrides():
    pricing.set_overrides({})
    yield
    pricing.set_overrides({})


def test_current_models_are_priced():
    for mid in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5"):
        assert pricing.lookup_price(mid) is not None, mid


def test_dated_snapshot_falls_back_to_base_id():
    assert pricing.lookup_price("claude-haiku-4-5-20251001") == pricing.lookup_price("claude-haiku-4-5")


def test_unknown_model_has_no_price():
    assert pricing.lookup_price("qwen3-14b-spec") is None
    assert pricing.lookup_price(None) is None


def test_estimate_cost_opus_5_5_cache_rates():
    price = pricing.lookup_price("claude-opus-5-5")
    # 1K base + 100K cache read + 10K 5m write + 2K 1h write, 2K out
    cost = pricing.estimate_cost(price, 113_000, 2_000, 100_000, 10_000, 2_000)
    expected = (1_000 * 4 + 100_000 * 0.20 + 10_000 * 5 + 2_000 * 8 + 2_000 * 20) / 1e6
    assert cost == pytest.approx(expected)


def test_estimate_cost_clamps_negative_base_input():
    price = pricing.lookup_price("claude-sonnet-5-5")
    cost = pricing.estimate_cost(price, 10, 0, 100, 0, 0)
    assert cost == pytest.approx(100 * 0.20 / 1e6)


def test_default_cache_read_is_tenth_of_input():
    price = pricing.ModelPrice(input=0.01, output=0.02)
    assert pricing.estimate_cost(price, 1000, 0, 1000) == pytest.approx(0.001)


def test_model_cost_prices_unlisted_claude_model():
    usage = {"input_tokens": 1000, "output_tokens": 1000}
    cost = app_module._model_cost(ModelConfig(model_id="claude-opus-5-5"), usage)
    assert cost == pytest.approx((1000 * 4 + 1000 * 20) / 1e6)


def test_model_cost_uses_cache_creation_split():
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 1000,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000},
    }
    cost = app_module._model_cost(ModelConfig(model_id="claude-opus-5-5"), usage)
    assert cost == pytest.approx(1000 * 8 / 1e6)


def test_configured_model_price_wins_over_table():
    cfg = ModelConfig(model_id="claude-opus-5-5", cost_per_1k_input=1.0, cost_per_1k_output=0.0)
    cost = app_module._model_cost(cfg, {"input_tokens": 1000})
    assert cost == pytest.approx(1.0)


def test_yaml_pricing_override_wins(tmp_path):
    p = tmp_path / "loom.yaml"
    p.write_text("pricing:\n  my-model:\n    input: 0.5\n    output: 1.0\n")
    LoomConfig.from_yaml(p)
    cost = app_module._model_cost(ModelConfig(model_id="my-model"), {"input_tokens": 1000, "output_tokens": 1000})
    assert cost == pytest.approx(1.5)


def test_local_model_costs_zero():
    assert app_module._model_cost(ModelConfig(model_id="qwen3-14b-spec"), {"input_tokens": 1000}) == 0.0


def test_stream_usage_keeps_cache_creation_split():
    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":5,'
        '"cache_creation_input_tokens":10,'
        '"cache_creation":{"ephemeral_5m_input_tokens":4,"ephemeral_1h_input_tokens":6}}}}',
        'data: {"type":"message_delta","usage":{"output_tokens":7}}',
    ]
    usage, _ = app_module._extract_stream_usage(lines)
    assert usage["cache_creation"]["ephemeral_1h_input_tokens"] == 6
