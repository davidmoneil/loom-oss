"""Tests for semantic-difficulty tier detection.

Regression cover for the length-bias bug: ``complexity_score`` used to derive
up to half its value from ``token_estimate`` alone, so verbose filler outscored
a short, genuinely hard request. A prompt asking for a correctness proof was
classified ``economy`` while 9000 repetitions of one word reached ``standard``.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.detection.engine import (
    DetectionEngine,
    extract_features,
    DEFAULT_ECONOMY_MAX_COMPLEXITY,
    DEFAULT_STANDARD_MAX_COMPLEXITY,
    LENGTH_MAX_CONTRIBUTION,
)


HARD_SHORT_PROOF = (
    "Prove whether this concurrent queue implementation is linearizable. "
    "If not, construct a counterexample execution history."
)
HARD_SHORT_DESIGN = (
    "Design a multi-region failover architecture and analyse the consistency "
    "tradeoffs."
)
LONG_FILLER = "la " * 9000


@pytest.fixture
def engine():
    return DetectionEngine()


# --- the original bug ------------------------------------------------------

def test_short_hard_prompt_reaches_premium(engine):
    """A short prompt demanding a proof is not an economy prompt."""
    assert engine.detect("t", HARD_SHORT_PROOF).recommended_tier == "premium"


def test_short_design_prompt_reaches_premium(engine):
    assert engine.detect("t", HARD_SHORT_DESIGN).recommended_tier == "premium"


def test_long_filler_does_not_reach_premium(engine):
    """Verbosity alone must never buy the most expensive tier."""
    assert engine.detect("t", LONG_FILLER).recommended_tier != "premium"


def test_filler_does_not_outrank_substance():
    """The core inversion: length used to beat difficulty. It must not."""
    filler = extract_features(LONG_FILLER).complexity_score
    proof = extract_features(HARD_SHORT_PROOF).complexity_score
    assert filler < proof, f"filler {filler} still outranks proof {proof}"


# --- difficulty is independent of length -----------------------------------

def test_difficulty_score_ignores_length():
    """difficulty_score is the length-proof component."""
    short = extract_features(HARD_SHORT_PROOF).difficulty_score
    padded = extract_features(HARD_SHORT_PROOF + " " + LONG_FILLER).difficulty_score
    assert short == padded


def test_filler_has_no_difficulty():
    assert extract_features(LONG_FILLER).difficulty_score == 0.0


def test_length_contribution_is_capped():
    """Length can never on its own push a prompt past the economy ceiling."""
    f = extract_features(LONG_FILLER)
    assert f.length_score <= LENGTH_MAX_CONTRIBUTION
    assert LENGTH_MAX_CONTRIBUTION <= DEFAULT_ECONOMY_MAX_COMPLEXITY


def test_two_reasoning_signals_clear_standard_without_markup():
    """Real prompts carry no XML tags, so difficulty must suffice unaided."""
    f = extract_features(HARD_SHORT_PROOF)
    assert f.high_complexity_tag_count == 0
    assert f.difficulty_score > DEFAULT_STANDARD_MAX_COMPLEXITY


# --- tier separation -------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "What's the capital of France?",
    "Translate 'good morning' into Spanish.",
    "Fix the typo: 'recieve the pacakge'",
    "Summarise this in one sentence: the meeting moved to Thursday.",
])
def test_trivial_prompts_stay_economy(engine, prompt):
    assert engine.detect("t", prompt).recommended_tier == "economy"


@pytest.mark.parametrize("prompt", [
    "Write a Python function that parses an ISO timestamp and returns the weekday name.",
    "Explain the difference between a process and a thread to a junior engineer.",
    "Review this React component for accessibility issues and suggest fixes.",
])
def test_ordinary_work_is_standard(engine, prompt):
    assert engine.detect("t", prompt).recommended_tier == "standard"


@pytest.mark.parametrize("prompt", [
    "Audit this authentication flow and rank remediation by exploitability.",
    "Diagnose the root cause of this regression and design a fix.",
])
def test_reasoning_work_is_premium(engine, prompt):
    assert engine.detect("t", prompt).recommended_tier == "premium"


def test_trivial_marker_cancels_production_verb(engine):
    """'fix' lifts a prompt; 'typo' puts it back down."""
    assert engine.detect("t", "Fix the typo: 'recieve'").recommended_tier == "economy"
    assert engine.detect("t", "Fix the race condition in this worker pool "
                              "and add a regression test").recommended_tier != "economy"


def test_trivial_marker_never_suppresses_reasoning(engine):
    """A trivial word must not drag a genuine reasoning request down."""
    p = "Define the failure modes of this consensus protocol and prove liveness."
    assert engine.detect("t", p).recommended_tier == "premium"


# --- API compatibility -----------------------------------------------------

def test_new_feature_fields_default():
    """Positional construction predating the difficulty fields still works."""
    from loom.detection.engine import PromptFeatures
    f = PromptFeatures(100, 0, 0, 0, 1, False)
    assert f.premium_signal_count == 0
    assert f.difficulty_score == 0.0
    assert f.complexity_score >= 0.0


def test_reason_mentions_reasoning_signals(engine):
    r = engine.detect("t", HARD_SHORT_PROOF)
    assert "reasoning signals" in r.reason


def test_detection_result_shape_unchanged(engine):
    r = engine.detect("src", "What's 2+2?")
    assert r.source == "src"
    assert r.recommended_tier in ("economy", "standard", "premium")
    assert 0.0 <= r.confidence <= 100.0
    assert isinstance(r.reason, str) and r.reason
