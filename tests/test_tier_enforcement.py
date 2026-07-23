"""Compression tier enforcement: light/medium/heavy/extreme semantics."""

from loom.compression.tiers import resolve_tier
from loom.config import CompressionConfig, LoomConfig, SourcePolicy
from loom.gateway.app import (
    _compress_messages_inline,
    _strip_loom_tag,
    _tier_age_ratio,
)

FILLER = (
    "So basically what happened is that the deployment process, you know, "
    "actually completed successfully and everything worked fine in the end. "
) * 20


def _messages(n: int) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}: {FILLER}",
        }
        for i in range(n)
    ]


class TierProcessor:
    """Records effective ages; compresses by age band like the real one."""

    def __init__(self):
        self.ages: list[float] = []

    def compress_light(self, text: str) -> str:
        return text.replace("you know, ", "")

    def compress_graduated(self, text: str, age_ratio: float):
        self.ages.append(age_ratio)
        if age_ratio < 0.3:
            return text, "full"
        if age_ratio < 0.7:
            return text[: len(text) // 2], "medium"
        return text[: len(text) // 4], "heavy"


def test_tier_age_mapping():
    assert _tier_age_ratio(0.4, "medium") == 0.4
    assert _tier_age_ratio(0.4, "light") == 0.4
    assert abs(_tier_age_ratio(0.4, "heavy") - 0.75) < 1e-9
    assert _tier_age_ratio(0.9, "heavy") == 1.0
    assert _tier_age_ratio(0.1, "extreme") == 1.0


def test_tiers_produce_different_output():
    """default_tier measurably alters compression output per tier."""
    sizes = {}
    for tier in ("light", "medium", "heavy", "extreme"):
        out, before, after, _, _loop = _compress_messages_inline(
            TierProcessor(), _messages(8), tier_name=tier
        )
        sizes[tier] = sum(
            len(m["content"]) for m in out if isinstance(m["content"], str)
        )
    # Stronger tiers shrink the conversation strictly further.
    assert sizes["light"] > sizes["medium"] > sizes["heavy"] > sizes["extreme"]


def test_light_tier_filler_only():
    proc = TierProcessor()
    out, before, after, _, _loop = _compress_messages_inline(
        proc, _messages(8), tier_name="light"
    )
    # Light never invokes the graduated pass...
    assert proc.ages == []
    # ...but still removes filler and tags the result.
    _, tier = _strip_loom_tag(out[0]["content"])
    assert tier == "light"
    assert "you know, " not in _strip_loom_tag(out[0]["content"])[0]
    assert before > after


def test_heavy_shifts_age():
    proc = TierProcessor()
    _compress_messages_inline(proc, _messages(8), tier_name="heavy")
    # Every effective age is >= 0.35 and message 0's natural 0.0 became 0.35.
    assert proc.ages and min(proc.ages) >= 0.35


def test_extreme_forces_max_age_and_fingerprints_tool_results():
    msgs = _messages(8)
    msgs[4] = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_x", "content": FILLER},
        ],
    }
    proc = TierProcessor()
    out, before, after, by_type, _loop = _compress_messages_inline(
        proc, msgs, tier_name="extreme"
    )
    # Text messages all compressed at age 1.0.
    assert proc.ages and set(proc.ages) == {1.0}
    # tool_result replaced by fingerprint, envelope intact.
    block = out[4]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_x"
    assert block["content"].startswith("[tool_result fingerprint:")
    _, tier = _strip_loom_tag(block["content"])
    assert tier == "extreme"
    assert by_type["tool_result"]["after"] < by_type["tool_result"]["before"]


def test_extreme_fingerprint_not_refingerprinted():
    msgs = _messages(8)
    msgs[4] = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_x", "content": FILLER},
        ],
    }
    out1, _, _, _, _ = _compress_messages_inline(
        TierProcessor(), msgs, tier_name="extreme"
    )
    out2, _, _, _, _ = _compress_messages_inline(
        TierProcessor(), out1, tier_name="extreme"
    )
    assert out2[4]["content"] == out1[4]["content"]


def test_resolve_tier_priority():
    cfg = LoomConfig(
        compression=CompressionConfig(default_tier="heavy"),
        sources={"headless": SourcePolicy(compression_tier="extreme")},
    )
    # Header wins over everything.
    assert resolve_tier("headless", "light", cfg) == "light"
    # Source policy beats config default.
    assert resolve_tier("headless", None, cfg) == "extreme"
    # Config default when the source has no override.
    assert resolve_tier("other", None, cfg) == "heavy"
    # Invalid header falls through to source policy.
    assert resolve_tier("headless", "bogus", cfg) == "extreme"
