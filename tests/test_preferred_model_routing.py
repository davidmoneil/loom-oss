"""Tests for the `preferred_model` routing input (Fix D, 2026-09-26 decision:
"loom-oss owns routing"; a Pulse task's `model-preference:` tag is forwarded
as `preferred_model` in the request body).

`preferred_model` is a soft input, not a bypass: it is honored only when it
resolves to a known model that passes the source's existing policy (allowed
providers, tier floor/budget, tool support). It never short-circuits ahead of
an explicit client-specified model or a source pin, and when it can't be
honored, routing proceeds exactly as it would with no preference — the
returned recommendation's `routing_reason` just notes the override.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.config import LoomConfig, ModelConfig, ProviderConfig, SourcePolicy
from loom.gateway.app import _select_model, GatewayState
from loom.routing.engine import RoutingEngine


def make_config(minimum_tier="economy", budget_tier=None, allowed_providers=None,
                 requires_tools=False):
    return LoomConfig(
        providers=[
            ProviderConfig(
                name="anthropic",
                api_base="https://api.anthropic.com",
                models=[
                    ModelConfig(model_id="haiku", tier="economy",
                                cost_per_1k_input=0.001, cost_per_1k_output=0.001),
                    ModelConfig(model_id="sonnet", tier="standard",
                                cost_per_1k_input=0.01, cost_per_1k_output=0.01,
                                supports_tools=True),
                    ModelConfig(model_id="opus", tier="premium",
                                cost_per_1k_input=0.1, cost_per_1k_output=0.1,
                                supports_tools=False),
                ],
            ),
            ProviderConfig(
                name="ollama",
                api_base="http://localhost:11434",
                models=[
                    ModelConfig(model_id="qwen2.5:7b", display_name="qwen2.5:7b",
                                tier="economy", cost_per_1k_input=0.0,
                                cost_per_1k_output=0.0),
                ],
            ),
        ],
        sources={
            "default": SourcePolicy(
                minimum_tier=minimum_tier,
                budget_tier=budget_tier,
                requires_tools=requires_tools,
                **({"allowed_providers": allowed_providers} if allowed_providers else {}),
            )
        },
    )


def make_state(config, routing=None):
    state = GatewayState.__new__(GatewayState)
    state.config = config
    state.detection = None
    state.routing = routing
    return state


class TestRoutingEngineHonorsPreference:
    def test_eligible_preferred_model_is_honored(self):
        engine = RoutingEngine(make_config(minimum_tier="economy"))
        rec = engine.recommend(task_type="chat", source="default", preferred_model="opus")
        assert rec.model == "opus"
        assert rec.routing_reason == "preferred_honored"

    def test_no_preference_is_unaffected(self):
        engine = RoutingEngine(make_config(minimum_tier="economy"))
        rec = engine.recommend(task_type="chat", source="default")
        assert rec.model == "haiku"
        assert "preferred" not in rec.routing_reason

    def test_unknown_preferred_model_falls_back(self):
        engine = RoutingEngine(make_config(minimum_tier="economy"))
        rec = engine.recommend(
            task_type="chat", source="default", preferred_model="not-a-real-model"
        )
        assert rec.model == "haiku"
        assert "preferred_overridden:not-a-real-model" in rec.routing_reason

    def test_preferred_model_below_tier_floor_falls_back(self):
        engine = RoutingEngine(make_config(minimum_tier="premium"))
        rec = engine.recommend(task_type="chat", source="default", preferred_model="haiku")
        assert rec.model == "opus"
        assert "preferred_overridden:haiku" in rec.routing_reason

    def test_preferred_model_above_budget_tier_falls_back(self):
        engine = RoutingEngine(
            make_config(minimum_tier="economy", budget_tier="standard")
        )
        rec = engine.recommend(task_type="chat", source="default", preferred_model="opus")
        assert rec.model == "haiku"
        assert "preferred_overridden:opus" in rec.routing_reason

    def test_preferred_model_from_disallowed_provider_falls_back(self):
        engine = RoutingEngine(
            make_config(minimum_tier="economy", allowed_providers=["anthropic"])
        )
        rec = engine.recommend(
            task_type="chat", source="default", preferred_model="qwen2.5:7b"
        )
        assert rec.model == "haiku"
        assert "preferred_overridden:qwen2.5:7b" in rec.routing_reason

    def test_preferred_model_without_required_tool_support_falls_back(self):
        engine = RoutingEngine(make_config(minimum_tier="economy", requires_tools=True))
        rec = engine.recommend(
            task_type="chat", source="default", requires_tools=True,
            preferred_model="opus",
        )
        # opus doesn't support tools in this fixture; sonnet is the only
        # tool-capable candidate, so it wins the fallback.
        assert rec.model == "sonnet"
        assert "preferred_overridden:opus" in rec.routing_reason

    def test_preferred_model_honored_alongside_tier_floor(self):
        engine = RoutingEngine(make_config(minimum_tier="economy"))
        rec = engine.recommend(
            task_type="chat", source="default", min_tier_floor="standard",
            preferred_model="sonnet",
        )
        assert rec.model == "sonnet"
        assert rec.routing_reason == "preferred_honored"


class TestSelectModelPreferenceOrdering:
    def test_preferred_model_is_honored_when_no_pin_or_explicit(self):
        config = make_config(minimum_tier="economy")
        state = make_state(config, routing=RoutingEngine(config))
        model, task_type, reason = _select_model(
            state, None, "default", {"preferred_model": "opus"},
            [{"role": "user", "content": "hi"}],
        )
        assert model == "opus"
        assert reason == "preferred_honored"

    def test_explicit_client_model_bypasses_preference(self):
        config = make_config(minimum_tier="economy")
        state = make_state(config, routing=RoutingEngine(config))
        model, task_type, reason = _select_model(
            state, "haiku", "default", {"preferred_model": "opus"},
            [{"role": "user", "content": "hi"}],
        )
        assert model == "haiku"
        assert reason == "client_specified"

    def test_source_pin_bypasses_preference(self):
        config = make_config(minimum_tier="economy")
        config.sources["default"].pinned_model = "haiku"
        state = make_state(config, routing=RoutingEngine(config))
        model, task_type, reason = _select_model(
            state, None, "default", {"preferred_model": "opus"},
            [{"role": "user", "content": "hi"}],
        )
        assert model == "haiku"
        assert reason == "source_pinned"

    def test_blank_preferred_model_is_treated_as_no_preference(self):
        config = make_config(minimum_tier="economy")
        state = make_state(config, routing=RoutingEngine(config))
        model, task_type, reason = _select_model(
            state, None, "default", {"preferred_model": "  "},
            [{"role": "user", "content": "hi"}],
        )
        assert model == "haiku"
        assert "preferred" not in reason

    def test_missing_preferred_model_key_is_unaffected(self):
        config = make_config(minimum_tier="economy")
        state = make_state(config, routing=RoutingEngine(config))
        model, task_type, reason = _select_model(
            state, None, "default", {}, [{"role": "user", "content": "hi"}]
        )
        assert model == "haiku"
        assert reason == "config_fallback"
