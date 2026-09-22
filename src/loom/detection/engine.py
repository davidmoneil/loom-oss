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


# Vocabulary that signals genuine reasoning work: multi-step analysis, design
# under tradeoffs, proof, or long-horizon planning. These drive the difficulty
# score high enough to reach ``premium`` on their own, independent of length —
# a short prompt asking for a correctness proof is not an economy prompt.
_PREMIUM_PATTERNS = [
    r"\barchitect(?:ure|ing)?\b", r"\bdesign\b", r"\bprove\b", r"\bproof\b",
    r"\bderive\b", r"\bjustif(?:y|ication)\b", r"\bdefend\b",
    r"\banalys(?:e|is)\b", r"\banalyz(?:e|is)\b", r"\baudit\b",
    r"\bdiagnos(?:e|is)\b", r"\btrade[\s-]?offs?\b", r"\btrading off\b",
    r"\bcounterexample\b", r"\blineariz(?:able|ability)\b",
    r"\broot[\s-]cause\b", r"\bfailure[\s-]mode", r"\battack path\b",
    r"\bblast radius\b", r"\bremediation\b", r"\bexploitability\b",
    r"\bmigration\b", r"\brollback\b", r"\bzero[\s-]downtime\b",
    r"\bdeployable\b", r"\bdependency ordering\b", r"\bdecision tree\b",
    r"\battribute the\b", r"\bwalk through\b", r"\bsystematically\b",
    r"\breason through\b", r"\bstrongest objection\b", r"\btelemetry\b",
    r"\bconsistency\b", r"\bquantify\b", r"\bisolate\b",
    r"\bevaluate\b", r"\bweigh\b", r"\bassess\b", r"\bcompare\b",
    r"\brecommend(?:ation)?\b", r"\bstrategy\b", r"\brollout\b",
    r"\breconciliation\b", r"\bconstraints?\b", r"\bimplications?\b",
]

# Verbs that imply producing a non-trivial artifact: ordinary engineering and
# writing work. Enough to clear ``economy``, not enough to reach ``premium``.
_PRODUCTION_PATTERNS = [
    r"\bwrite\b", r"\bdraft\b", r"\bimplement\b", r"\bexplain\b",
    r"\breview\b", r"\brefactor\b", r"\bsummaris(?:e)\b", r"\bsummariz(?:e)\b",
    r"\bturn these\b", r"\bturn this\b", r"\badd\b", r"\bgenerate\b",
    r"\bcreate\b", r"\bbuild\b", r"\bfix\b", r"\boptimis(?:e)\b",
    r"\boptimiz(?:e)\b", r"\bsuggest\b",
]

# Markers of a trivial, single-step operation. These cancel a production verb
# so that e.g. "fix the typo" or "summarise in one sentence" stays economy.
_TRIVIAL_PATTERNS = [
    r"\btranslate\b", r"\bconvert\b", r"\breformat\b", r"\btypo\b",
    r"\bsynonym\b", r"\bantonym\b", r"\bin one sentence\b",
    r"\bcapital of\b", r"\bstand for\b", r"\bprime number\b",
    r"\bextract the\b", r"\bwho wrote\b", r"\bdefine\b",
]

# A second imperative joined by "and" indicates a multi-part request.
_MULTIPART_RE = re.compile(
    r"\band\s+(?:then\s+)?(?:list|suggest|propose|recommend|make|return|"
    r"report|give|produce|rank|state|defend|explain|add|write|construct|"
    r"quantify|design)\b",
    re.IGNORECASE,
)

_PREMIUM_RE = re.compile("|".join(_PREMIUM_PATTERNS), re.IGNORECASE)
_PRODUCTION_RE = re.compile("|".join(_PRODUCTION_PATTERNS), re.IGNORECASE)
_TRIVIAL_RE = re.compile("|".join(_TRIVIAL_PATTERNS), re.IGNORECASE)

# Difficulty weights. A single premium signal (34) lands in standard — one
# reasoning verb is ambiguous on its own. Two clear STANDARD_MAX (65)
# unaided, which matters because real prompts carry no XML markup to top
# them up. Length contributes at most LENGTH_MAX_CONTRIBUTION,
# reduced from the original 50 so that verbosity alone cannot outrank
# substance — 9000 words of filler must not outscore a proof request.
PREMIUM_SIGNAL_WEIGHT = 34.0
PREMIUM_SIGNAL_CAP = 68.0
PRODUCTION_SIGNAL_WEIGHT = 32.0
PRODUCTION_SIGNAL_CAP = 32.0
TRIVIAL_SIGNAL_PENALTY = 18.0
MULTIPART_WEIGHT = 10.0
MULTIPART_CAP = 20.0
HIGH_COMPLEXITY_TAG_WEIGHT = 9.0
HIGH_COMPLEXITY_TAG_CAP = 27.0
LENGTH_MAX_CONTRIBUTION = 30.0


@dataclass
class PromptFeatures:
    token_estimate: int
    xml_tag_count: int
    high_complexity_tag_count: int
    tool_call_patterns: int
    message_count: int
    has_code_blocks: bool
    # Semantic-difficulty counts. Defaulted so existing positional callers and
    # tests that predate these fields keep working.
    premium_signal_count: int = 0
    production_signal_count: int = 0
    trivial_signal_count: int = 0
    multipart_count: int = 0

    @property
    def difficulty_score(self) -> float:
        """0-100 score for semantic difficulty, ignoring prompt length entirely.

        This is the part that length cannot buy. A prompt earns premium-range
        difficulty by asking for reasoning work — proof, design under
        tradeoffs, root-cause analysis — not by being long.
        """
        score = 0.0
        score += min(PREMIUM_SIGNAL_CAP,
                     self.premium_signal_count * PREMIUM_SIGNAL_WEIGHT)
        score += min(HIGH_COMPLEXITY_TAG_CAP,
                     self.high_complexity_tag_count * HIGH_COMPLEXITY_TAG_WEIGHT)
        score += min(MULTIPART_CAP, self.multipart_count * MULTIPART_WEIGHT)

        # Production verbs lift a prompt out of economy, but a trivial marker
        # ("fix the TYPO", "summarise IN ONE SENTENCE") cancels that lift.
        # Trivial markers never suppress genuine reasoning signals.
        if self.premium_signal_count == 0:
            production = min(PRODUCTION_SIGNAL_CAP,
                             self.production_signal_count * PRODUCTION_SIGNAL_WEIGHT)
            if self.trivial_signal_count:
                production = max(
                    0.0, production - self.trivial_signal_count * TRIVIAL_SIGNAL_PENALTY
                )
            score += production

        if self.has_code_blocks:
            score += 5
        score += min(12, self.tool_call_patterns * 3)
        return min(100.0, score)

    @property
    def length_score(self) -> float:
        """0-``LENGTH_MAX_CONTRIBUTION`` contribution from sheer prompt size."""
        return min(
            LENGTH_MAX_CONTRIBUTION,
            self.token_estimate / LONG_PROMPT_TOKENS * LENGTH_MAX_CONTRIBUTION,
        )

    @property
    def complexity_score(self) -> float:
        """0-100 complexity score; higher = more complex = prefer a higher tier."""
        score = self.difficulty_score + self.length_score
        score += min(10, self.xml_tag_count * 0.5)
        return min(100.0, score)


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

    premium_signals = len(set(m.group(0).lower()
                              for m in _PREMIUM_RE.finditer(prompt_text)))
    production_signals = len(set(m.group(0).lower()
                                 for m in _PRODUCTION_RE.finditer(prompt_text)))
    trivial_signals = len(set(m.group(0).lower()
                              for m in _TRIVIAL_RE.finditer(prompt_text)))
    multipart = len(_MULTIPART_RE.findall(prompt_text))

    return PromptFeatures(
        token_estimate=token_estimate,
        xml_tag_count=len(all_tags),
        high_complexity_tag_count=high_complexity,
        tool_call_patterns=tool_patterns,
        message_count=message_count,
        has_code_blocks=has_code,
        premium_signal_count=premium_signals,
        production_signal_count=production_signals,
        trivial_signal_count=trivial_signals,
        multipart_count=multipart,
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
        if features.premium_signal_count > 0:
            reasons.append(
                f"{features.premium_signal_count} reasoning signals "
                f"(difficulty {features.difficulty_score:.0f}/100)"
            )
        if features.multipart_count > 0:
            reasons.append(f"{features.multipart_count} multi-part requests")
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
