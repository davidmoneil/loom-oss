"""Detection engine for model-tier routing.

Analyzes prompt features (length, structured-output markers, tool-call
patterns, code blocks) to recommend a model tier — ``economy``, ``standard``,
or ``premium`` — with a confidence score and a human-readable reason.

Unlike the internal version, this engine has no cost-ledger dependency: tier
selection is driven purely by a fast, rule-based complexity analysis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

CHARS_PER_TOKEN = 4  # rough approximation, ~4 chars/token

# Default complexity thresholds (0-100). Below ECONOMY_MAX -> economy;
# below STANDARD_MAX -> standard; otherwise premium.
DEFAULT_ECONOMY_MAX_COMPLEXITY = 30.0
DEFAULT_STANDARD_MAX_COMPLEXITY = 65.0

# Prompts longer than this (estimated tokens) are biased toward higher tiers.
LONG_PROMPT_TOKENS = 8_000

# XML tags that signal high-complexity structured output.
_HIGH_COMPLEXITY_TAGS = frozenset([
    "artifact", "orchestration", "routing_decision", "analysis",
    "implementation", "architecture", "plan", "design",
])


@dataclass
class PromptFeatures:
    token_estimate: int
    xml_tag_count: int
    high_complexity_tag_count: int
    tool_call_patterns: int
    message_count: int
    has_code_blocks: bool

    @property
    def complexity_score(self) -> float:
        """0-100 complexity score; higher = more complex = prefer a higher tier."""
        score = 0.0
        score += min(50, self.token_estimate / LONG_PROMPT_TOKENS * 50)
        score += min(20, self.high_complexity_tag_count * 4)
        score += min(15, self.tool_call_patterns * 3)
        score += min(10, self.xml_tag_count * 0.5)
        if self.has_code_blocks:
            score += 5
        return min(100, score)


@dataclass
class DetectionResult:
    source: str
    recommended_tier: str       # "economy" | "standard" | "premium"
    confidence: float           # 0-100
    reason: str
    features: PromptFeatures


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def extract_features(prompt_text: str, message_count: int = 1) -> PromptFeatures:
    """Extract routing-relevant features from a prompt (fast, <10ms)."""
    token_estimate = _estimate_tokens(prompt_text)

    all_tags = re.findall(r"<([a-zA-Z][a-zA-Z0-9_-]*)", prompt_text)
    high_complexity = sum(1 for t in all_tags if t.lower() in _HIGH_COMPLEXITY_TAGS)

    tool_patterns = len(re.findall(
        r'(?:tool_use|ToolUse|<tool_call|"type":\s*"tool_use")',
        prompt_text,
    ))
    has_code = bool(re.search(r"```[\w]*\n", prompt_text))

    return PromptFeatures(
        token_estimate=token_estimate,
        xml_tag_count=len(all_tags),
        high_complexity_tag_count=high_complexity,
        tool_call_patterns=tool_patterns,
        message_count=message_count,
        has_code_blocks=has_code,
    )


# ---------------------------------------------------------------------------
# Task type classification (rule-based, <10ms)
# ---------------------------------------------------------------------------

_TASK_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("summarization", re.compile(r'\b(?:summar(?:ize|y|ise)|tl;?dr|condense|recap)\b', re.IGNORECASE)),
    ("code_generation", re.compile(r'\b(?:write|implement|refactor|generate)\b.*\b(?:function|code|class|method|script|program)\b|\b(?:def |class )', re.IGNORECASE)),
    ("extraction", re.compile(r'\b(?:extract|parse|pull out|scrape|pull the)\b', re.IGNORECASE)),
    ("classification", re.compile(r'\b(?:classif(?:y|ication)|categor(?:ize|ise|y)|label|tag this|which category)\b', re.IGNORECASE)),
    ("json_generation", re.compile(r'\b(?:json|schema|structured output|valid json|as json)\b', re.IGNORECASE)),
    ("translation", re.compile(r'\btranslate\b', re.IGNORECASE)),
]


def classify_task_type(messages: list[dict]) -> str:
    """Classify a task type from chat messages using fast keyword rules.

    Looks at the most recent user message. Returns one of: ``summarization``,
    ``code_generation``, ``extraction``, ``classification``,
    ``json_generation``, ``translation``, or ``general``.
    """
    text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # Anthropic-style content blocks.
                content = " ".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict)
                )
            text = str(content)
            break
    if not text:
        # Fall back to concatenating all message text.
        text = " ".join(str(m.get("content", "")) for m in messages)

    for task_type, pattern in _TASK_PATTERNS:
        if pattern.search(text):
            return task_type
    return "general"


# ---------------------------------------------------------------------------
# Detection engine
# ---------------------------------------------------------------------------

class DetectionEngine:
    """Maps prompt complexity to a recommended model tier."""

    def __init__(self, config=None):
        self._config = config
        self._economy_max = DEFAULT_ECONOMY_MAX_COMPLEXITY
        self._standard_max = DEFAULT_STANDARD_MAX_COMPLEXITY

    def detect(self, source: str, prompt_text: str,
               message_count: int = 1) -> DetectionResult:
        """Return a tier recommendation with a 0-100 confidence score."""
        features = extract_features(prompt_text, message_count)
        complexity = features.complexity_score

        # Long prompts are biased upward: cap how "economy" a long prompt can be.
        if features.token_estimate > LONG_PROMPT_TOKENS:
            complexity = max(complexity, self._economy_max + 1)

        if complexity <= self._economy_max:
            tier = "economy"
            # Confidence grows as complexity falls below the economy ceiling.
            confidence = 100.0 - (complexity / self._economy_max * 50.0)
        elif complexity <= self._standard_max:
            tier = "standard"
            span = self._standard_max - self._economy_max
            midpoint = self._economy_max + span / 2
            confidence = 100.0 - (abs(complexity - midpoint) / (span / 2) * 40.0)
        else:
            tier = "premium"
            span = 100.0 - self._standard_max
            confidence = 60.0 + ((complexity - self._standard_max) / span * 40.0)

        reasons = []
        if features.token_estimate > LONG_PROMPT_TOKENS:
            reasons.append(f"long prompt ({features.token_estimate} tokens)")
        if features.complexity_score > self._standard_max:
            reasons.append(f"high complexity ({features.complexity_score:.0f}/100)")
        elif features.complexity_score <= self._economy_max:
            reasons.append(f"low complexity ({features.complexity_score:.0f}/100)")
        if features.high_complexity_tag_count > 0:
            reasons.append(f"{features.high_complexity_tag_count} complex XML tags")
        if features.tool_call_patterns > 0:
            reasons.append(f"{features.tool_call_patterns} tool-call patterns")
        if features.has_code_blocks:
            reasons.append("contains code blocks")
        if not reasons:
            reasons.append("default routing")

        return DetectionResult(
            source=source,
            recommended_tier=tier,
            confidence=round(max(0.0, min(100.0, confidence)), 1),
            reason="; ".join(reasons),
            features=features,
        )
