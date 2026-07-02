"""Tests for compression tier resolution and tag round-tripping."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.compression.tiers import (
    VALID_TIERS,
    DEFAULT_TIER,
    resolve_tier,
    age_floor_for_tier,
    tier_level,
    strip_loom_tag,
    add_loom_tag,
    content_hash,
    compute_context_pressure,
)


class FakePolicy:
    def __init__(self, compression_tier=None):
        self.compression_tier = compression_tier


class FakeCompressionConfig:
    def __init__(self, default_tier=""):
        self.default_tier = default_tier


class FakeConfig:
    def __init__(self, source_policy=None, default_tier=""):
        self._source_policy = source_policy
        self.compression = FakeCompressionConfig(default_tier)

    def get_source_policy(self, source):
        return self._source_policy


# ============================================================ resolve_tier priority chain

class TestResolveTierPriority:
    def test_request_override_wins(self):
        config = FakeConfig(source_policy=FakePolicy("light"), default_tier="heavy")
        assert resolve_tier(source="x", request_override="extreme", config=config) == "extreme"

    def test_invalid_override_falls_through(self):
        config = FakeConfig(source_policy=FakePolicy("light"))
        assert resolve_tier(source="x", request_override="not-a-tier", config=config) == "light"

    def test_source_policy_wins_over_config_default(self):
        config = FakeConfig(source_policy=FakePolicy("heavy"), default_tier="medium")
        assert resolve_tier(source="x", config=config) == "heavy"

    def test_config_default_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("LOOM_COMPRESSION_TIER", "extreme")
        config = FakeConfig(source_policy=None, default_tier="light")
        assert resolve_tier(source="x", config=config) == "light"

    def test_env_var_used_when_no_config_default(self, monkeypatch):
        monkeypatch.setenv("LOOM_COMPRESSION_TIER", "heavy")
        config = FakeConfig(source_policy=None, default_tier="")
        assert resolve_tier(source="x", config=config) == "heavy"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("LOOM_COMPRESSION_TIER", raising=False)
        assert resolve_tier() == DEFAULT_TIER

    def test_no_config_no_source(self, monkeypatch):
        monkeypatch.delenv("LOOM_COMPRESSION_TIER", raising=False)
        assert resolve_tier(source="", request_override=None, config=None) == DEFAULT_TIER


# ============================================================ tier metadata

class TestTierMetadata:
    @pytest.mark.parametrize("tier", VALID_TIERS)
    def test_age_floor_known_tiers(self, tier):
        assert 0.0 <= age_floor_for_tier(tier) <= 1.0

    def test_age_floor_unknown_tier_falls_back_to_default(self):
        assert age_floor_for_tier("bogus") == age_floor_for_tier(DEFAULT_TIER)

    def test_tier_level_ordering(self):
        assert tier_level("light") < tier_level("medium") < tier_level("heavy") < tier_level("extreme")

    def test_tier_level_unknown_falls_back_to_default(self):
        assert tier_level("bogus") == tier_level(DEFAULT_TIER)


# ============================================================ tag round-trip

class TestTagRoundTrip:
    @pytest.mark.parametrize("tier", VALID_TIERS)
    def test_add_then_strip_round_trip(self, tier):
        original = "some compressed content"
        h = content_hash(original)
        tagged = add_loom_tag(original, tier, h)
        stripped, found_tier, found_hash = strip_loom_tag(tagged)
        assert stripped == original
        assert found_tier == tier
        assert found_hash == h

    def test_strip_without_tag_returns_none(self):
        text = "plain untagged content"
        stripped, tier, h = strip_loom_tag(text)
        assert stripped == text
        assert tier is None
        assert h is None

    def test_strip_trailing_whitespace_tolerant(self):
        h = content_hash("body")
        tagged = add_loom_tag("body", "medium", h) + "\n\n  "
        stripped, tier, found_hash = strip_loom_tag(tagged)
        assert stripped == "body"
        assert tier == "medium"
        assert found_hash == h

    def test_content_hash_deterministic(self):
        assert content_hash("same text") == content_hash("same text")

    def test_content_hash_differs(self):
        assert content_hash("text a") != content_hash("text b")


# ============================================================ context pressure

class TestContextPressure:
    def test_short_conversation_no_pressure(self):
        messages = [{"role": "user", "content": "hi"}] * 5
        assert compute_context_pressure(messages) == 0.0

    def test_long_conversation_has_pressure(self):
        messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x"} for i in range(60)]
        assert compute_context_pressure(messages) > 0.0

    def test_pressure_bounded_at_one(self):
        messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x"} for i in range(500)]
        assert compute_context_pressure(messages) <= 1.0

    def test_system_messages_dont_count_toward_conv_length(self):
        messages = [{"role": "system", "content": "x"}] * 20
        assert compute_context_pressure(messages) == 0.0
