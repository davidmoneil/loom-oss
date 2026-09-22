"""Tests for wiring DetectionEngine's tier estimate into routing (AIProjects-nyah).

Detection has been diagnostic-only: /v1/detect computed a tier that nothing
read. These tests cover the new minimum_tier-floor mechanism: a detected
tier can raise a source's effective minimum_tier for one recommendation,
but never overrides an explicit pin or client-specified model, and is off
by default.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.config import LoomConfig, ModelConfig, ProviderConfig, SourcePolicy
from loom.gateway.app import _detect_tier_floor, _select_model, GatewayState
from loom.routing.engine import RoutingEngine


def make_config(minimum_tier="economy"):
    return LoomConfig(
        providers=[
            ProviderConfig(
                name="anthropic",
                api_base="https://api.anthropic.com",
                models=[
                    ModelConfig(model_id="haiku", tier="economy",
                                cost_per_1k_input=0.001, cost_per_1k_output=0.001),
                    ModelConfig(model_id="sonnet", tier="standard",
                                cost_per_1k_input=0.01, cost_per_1k_output=0.01),
                    ModelConfig(model_id="opus", tier="premium",
                                cost_per_1k_input=0.1, cost_per_1k_output=0.1),
                ],
            )
        ],
        sources={"default": SourcePolicy(minimum_tier=minimum_tier)},
    )


class TestRoutingEngineTierFloor:
    def test_no_floor_picks_cheapest_eligible_model(self):
        engine = RoutingEngine(make_config(minimum_tier="economy"))
        rec = engine.recommend(task_type="chat", source="default")
        assert rec.model == "haiku"

    def test_floor_raises_effective_minimum_tier(self):
        engine = RoutingEngine(make_config(minimum_tier="economy"))
        rec = engine.recommend(task_type="chat", source="default", min_tier_floor="premium")
        assert rec.model == "opus"

    def test_floor_never_lowers_an_already_higher_policy_minimum(self):
        engine = RoutingEngine(make_config(minimum_tier="premium"))
        rec = engine.recommend(task_type="chat", source="default", min_tier_floor="economy")
        assert rec.model == "opus"

    def test_unknown_floor_value_is_ignored(self):
        engine = RoutingEngine(make_config(minimum_tier="economy"))
        rec = engine.recommend(task_type="chat", source="default", min_tier_floor="not-a-tier")
        assert rec.model == "haiku"


class FakeDetection:
    def __init__(self, tier, confidence):
        self._tier = tier
        self._confidence = confidence

    def detect(self, source, prompt):
        return {"recommended_tier": self._tier, "confidence": self._confidence}


def make_state(config, detection=None, routing=None):
    state = GatewayState.__new__(GatewayState)
    state.config = config
    state.detection = detection
    state.routing = routing
    return state


class TestDetectTierFloor:
    def test_disabled_by_default_returns_none(self):
        config = make_config()
        state = make_state(config, detection=FakeDetection("premium", 0.99))
        assert _detect_tier_floor(state, "default", [{"role": "user", "content": "hi"}]) is None

    def test_enabled_but_low_confidence_returns_none(self):
        config = make_config()
        config.routing.detection_routing_enabled = True
        config.routing.detection_routing_min_confidence = 0.8
        state = make_state(config, detection=FakeDetection("premium", 0.3))
        assert _detect_tier_floor(state, "default", [{"role": "user", "content": "hi"}]) is None

    def test_enabled_and_confident_returns_tier(self):
        config = make_config()
        config.routing.detection_routing_enabled = True
        config.routing.detection_routing_min_confidence = 0.5
        state = make_state(config, detection=FakeDetection("premium", 0.9))
        assert _detect_tier_floor(state, "default", [{"role": "user", "content": "hi"}]) == "premium"

    def test_no_detection_engine_returns_none(self):
        config = make_config()
        config.routing.detection_routing_enabled = True
        state = make_state(config, detection=None)
        assert _detect_tier_floor(state, "default", [{"role": "user", "content": "hi"}]) is None

    def test_empty_prompt_returns_none(self):
        config = make_config()
        config.routing.detection_routing_enabled = True
        state = make_state(config, detection=FakeDetection("premium", 0.9))
        assert _detect_tier_floor(state, "default", []) is None


class TestSelectModelRespectsPins:
    def test_explicit_client_model_bypasses_detection_floor(self):
        config = make_config(minimum_tier="economy")
        config.routing.detection_routing_enabled = True
        config.routing.detection_routing_min_confidence = 0.0
        state = make_state(
            config,
            detection=FakeDetection("premium", 0.99),
            routing=RoutingEngine(config),
        )
        model, task_type, reason = _select_model(
            state, "haiku", "default", {}, [{"role": "user", "content": "hi"}]
        )
        assert model == "haiku"
        assert reason == "client_specified"

    def test_source_pin_bypasses_detection_floor(self):
        config = make_config(minimum_tier="economy")
        config.sources["default"].pinned_model = "haiku"
        config.routing.detection_routing_enabled = True
        config.routing.detection_routing_min_confidence = 0.0
        state = make_state(
            config,
            detection=FakeDetection("premium", 0.99),
            routing=RoutingEngine(config),
        )
        model, task_type, reason = _select_model(
            state, None, "default", {}, [{"role": "user", "content": "hi"}]
        )
        assert model == "haiku"
        assert reason == "source_pinned"

    def test_auto_routing_applies_confident_detected_floor(self):
        config = make_config(minimum_tier="economy")
        config.routing.detection_routing_enabled = True
        config.routing.detection_routing_min_confidence = 0.5
        state = make_state(
            config,
            detection=FakeDetection("premium", 0.9),
            routing=RoutingEngine(config),
        )
        model, task_type, reason = _select_model(
            state, "auto", "default", {}, [{"role": "user", "content": "hi"}]
        )
        assert model == "opus"
        assert "detected_tier_floor:premium" in reason

    def test_auto_routing_ignores_unconfident_detection(self):
        config = make_config(minimum_tier="economy")
        config.routing.detection_routing_enabled = True
        config.routing.detection_routing_min_confidence = 0.8
        state = make_state(
            config,
            detection=FakeDetection("premium", 0.2),
            routing=RoutingEngine(config),
        )
        model, task_type, reason = _select_model(
            state, "auto", "default", {}, [{"role": "user", "content": "hi"}]
        )
        assert model == "haiku"
        assert "detected_tier_floor" not in reason
