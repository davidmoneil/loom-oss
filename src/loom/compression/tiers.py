"""Compression tier configuration and tag management.

Tier definitions:
  light   — filler removal only; age_floor 0.5
  medium  — graduated age-based compression; default; age_floor 0.3
  heavy   — age_ratio shifted +0.35 so medium-age content gets heavy treatment
  extreme — always apply heavy + replace tool outputs with fingerprints; age_floor 0.0

Tier resolution priority:
  1. Per-request x-loom-compression header
  2. Per-source policy override (config sources.<name>.compression_tier)
  3. Config compression.default_tier
  4. LOOM_COMPRESSION_TIER env var
  5. Default: medium

Compression tags (<!--loom:compressed:TIER:HASH-->) appended to compressed
content prevent re-compression on subsequent API calls.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

VALID_TIERS = ("light", "medium", "heavy", "extreme")
DEFAULT_TIER = "medium"

TIER_AGE_FLOOR: dict[str, float] = {
    "light": 0.5,
    "medium": 0.3,
    "heavy": 0.1,
    "extreme": 0.0,
}

TIER_LEVEL: dict[str, int] = {
    "light": 0,
    "medium": 1,
    "heavy": 2,
    "extreme": 3,
}

_TAG_RE = re.compile(r"<!--loom:compressed:(\w+):([a-f0-9]{8,16})-->$")


def resolve_tier(
    source: str = "",
    request_override: Optional[str] = None,
    config=None,
) -> str:
    if request_override and request_override in VALID_TIERS:
        return request_override

    if config is not None and source:
        policy = config.get_source_policy(source) if hasattr(config, "get_source_policy") else None
        if policy is not None:
            tier = getattr(policy, "compression_tier", None)
            if tier and tier in VALID_TIERS:
                return tier

    if config is not None:
        comp = getattr(config, "compression", None)
        if comp is not None:
            default = getattr(comp, "default_tier", "")
            if default and default in VALID_TIERS:
                return default

    env_tier = os.environ.get("LOOM_COMPRESSION_TIER", "").lower()
    if env_tier in VALID_TIERS:
        return env_tier

    return DEFAULT_TIER


def age_floor_for_tier(tier: str) -> float:
    return TIER_AGE_FLOOR.get(tier, TIER_AGE_FLOOR[DEFAULT_TIER])


def tier_level(tier: str) -> int:
    return TIER_LEVEL.get(tier, TIER_LEVEL[DEFAULT_TIER])


def strip_loom_tag(text: str) -> tuple[str, Optional[str], Optional[str]]:
    """Strip a loom:compressed tag if present.

    Returns (stripped_text, tier, content_hash), or (text, None, None).
    """
    m = _TAG_RE.search(text.rstrip())
    if m:
        tier_name = m.group(1)
        h = m.group(2)
        stripped = text[: m.start()].rstrip()
        return stripped, tier_name, h
    return text, None, None


def add_loom_tag(text: str, tier_name: str, content_hash: str) -> str:
    return f"{text}\n<!--loom:compressed:{tier_name}:{content_hash}-->"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def compute_context_pressure(messages: list[dict]) -> float:
    """Estimate context window pressure as a 0.0-1.0 score.

    Combines conversation length with age distribution. Both signals above
    8 messages are needed to trigger, preventing false positives on short
    exchanges.
    """
    n = len(messages)
    if n < 8:
        return 0.0

    n_conv = sum(1 for m in messages if m.get("role") in ("user", "assistant"))
    if n_conv < 8:
        return 0.0

    length_pressure = min(1.0, (n_conv - 8) / 40.0)

    old_count = sum(
        1
        for i, m in enumerate(messages)
        if m.get("role") in ("user", "assistant") and (1.0 - i / n) > 0.4
    )
    age_pressure = old_count / max(n_conv, 1)

    return min(1.0, length_pressure * 0.6 + age_pressure * 0.4)
