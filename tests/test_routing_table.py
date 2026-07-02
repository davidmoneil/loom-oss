"""Tests for the EQRT (Eliminate-Qualify-Rank-Tiebreak) routing table algorithm."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.routing.models import RoutingEntry, RoutingTable, SourceProfile
from loom.routing.providers import ProviderRegistry


def make_entry(model, backend="anthropic", task_type="chat", determinism=0.8,
                ci_lo=None, ci_hi=None, num_runs=20):
    ci_lo = determinism - 0.05 if ci_lo is None else ci_lo
    ci_hi = determinism + 0.05 if ci_hi is None else ci_hi
    return RoutingEntry(
        model=model,
        backend=backend,
        task_type=task_type,
        temperature=0.0,
        seed_strategy="none",
        constraint_level_min=1,
        determinism_score=determinism,
        determinism_ci_lo=ci_lo,
        determinism_ci_hi=ci_hi,
        lexical_score=determinism,
        structural_score=determinism,
        semantic_score=determinism,
        exact_match_pct=determinism,
        num_runs=num_runs,
    )


def registry():
    return ProviderRegistry.default()


class TestPinning:
    def test_pinned_model_in_table_short_circuits(self):
        table = RoutingTable(entries=[
            make_entry("sonnet", num_runs=50, determinism=0.9),
            make_entry("haiku", num_runs=50, determinism=0.99),
        ])
        profile = SourceProfile(name="src", pinned_model="sonnet")
        rec = table.recommend(task_type="chat", source_profile=profile, registry=registry())
        assert rec.model == "sonnet"
        assert rec.routing_reason == "source_pin:src->sonnet"

    def test_pinned_model_absent_from_table_falls_back(self):
        table = RoutingTable(entries=[make_entry("sonnet")])
        profile = SourceProfile(name="src", pinned_model="some-unlisted-model")
        rec = table.recommend(task_type="chat", source_profile=profile, registry=registry())
        assert rec.model == "some-unlisted-model"
        assert rec.is_empirical is False


class TestEliminateGates:
    def test_falls_back_to_default_task_type(self):
        table = RoutingTable(entries=[make_entry("sonnet", task_type="_default")])
        profile = SourceProfile(name="src")
        rec = table.recommend(task_type="summarization", source_profile=profile, registry=registry())
        assert rec is not None
        assert rec.model == "sonnet"
        assert "default_fallback" in rec.routing_reason

    def test_no_matching_task_type_and_no_default_returns_none(self):
        table = RoutingTable(entries=[make_entry("sonnet", task_type="chat")])
        profile = SourceProfile(name="src")
        rec = table.recommend(task_type="summarization", source_profile=profile, registry=registry())
        assert rec is None

    def test_allowed_providers_gate_excludes_disallowed_backend(self):
        table = RoutingTable(entries=[
            make_entry("sonnet", backend="anthropic"),
            make_entry("qwen2.5:7b", backend="ollama"),
        ])
        profile = SourceProfile(name="src", allowed_providers=["ollama"])
        rec = table.recommend(task_type="chat", source_profile=profile, registry=registry())
        assert rec.model == "qwen2.5:7b"


class TestQualify:
    def test_tested_models_beat_untested_despite_lower_determinism(self):
        table = RoutingTable(entries=[
            make_entry("sonnet", determinism=0.6, num_runs=50),   # tested
            make_entry("haiku", determinism=0.99, num_runs=2),    # untested
        ])
        profile = SourceProfile(name="src")
        rec = table.recommend(task_type="chat", source_profile=profile, registry=registry())
        assert rec.model == "sonnet"

    def test_untested_pool_used_when_nothing_tested(self):
        table = RoutingTable(entries=[
            make_entry("sonnet", determinism=0.6, num_runs=2),
            make_entry("haiku", determinism=0.99, num_runs=2),
        ])
        profile = SourceProfile(name="src")
        rec = table.recommend(task_type="chat", source_profile=profile, registry=registry())
        assert "untested_pool" in rec.routing_reason


class TestRankAndTiebreak:
    def test_cheaper_model_wins_when_quality_tied(self):
        table = RoutingTable(entries=[
            make_entry("sonnet", determinism=0.9, ci_lo=0.85, num_runs=50),
            make_entry("haiku", determinism=0.9, ci_lo=0.85, num_runs=50),
        ])
        profile = SourceProfile(name="src")
        rec = table.recommend(task_type="chat", source_profile=profile, registry=registry())
        assert rec.model == "haiku"  # cheaper per-1k cost among equal quality

    def test_higher_quality_wins_even_if_more_expensive(self):
        table = RoutingTable(entries=[
            make_entry("sonnet", determinism=0.95, ci_lo=0.9, num_runs=50),
            make_entry("haiku", determinism=0.5, ci_lo=0.45, num_runs=50),
        ])
        profile = SourceProfile(name="src")
        rec = table.recommend(task_type="chat", source_profile=profile, registry=registry())
        assert rec.model == "sonnet"

    def test_alternatives_populated(self):
        table = RoutingTable(entries=[
            make_entry("sonnet", determinism=0.9, num_runs=50),
            make_entry("haiku", determinism=0.8, num_runs=50),
        ])
        profile = SourceProfile(name="src")
        rec = table.recommend(task_type="chat", source_profile=profile, registry=registry())
        assert len(rec.alternatives) == 1
        assert rec.alternatives[0].model == "haiku"
