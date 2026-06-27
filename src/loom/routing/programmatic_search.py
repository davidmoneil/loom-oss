"""Zero-inference routing tier — programmatic search before any LLM call.

Detects search-shaped prompts (find/where/locate patterns) and executes
ripgrep against registered source locations. High-confidence hits return
tier="zero-inference" so callers can skip the LLM entirely.

Escalation policy:
  - zero results       -> fall through to LLM routing
  - 1-20 results       -> return tier="zero-inference" + hits
  - >20 results        -> fall through (too broad; LLM synthesizes)
  - low intent score   -> fall through to normal routing
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SEARCH_INTENT_THRESHOLD = 75
MIN_HITS = 1
MAX_HITS_ZERO_INFERENCE = 20
RIPGREP_MAX_MATCHES = 50
RIPGREP_TIMEOUT_S = 3.0

_SEARCH_VERBS = frozenset([
    "find", "where", "locate", "search", "which", "grep",
    "look for", "list all", "show me", "what file", "what files",
    "where is", "where are", "what is the path", "where can i find",
])

_REASONING_MARKERS = frozenset([
    "why", "explain", "how does", "what should", "best practice",
    "compare", "difference between", "recommend", "pros and cons",
    "step by step", "implement", "design", "plan", "analyze",
    "think about", "reason",
])

_FILE_PATH_PATTERN = re.compile(r"[\w\-]+/[\w\-./]+ | [\w\-]+\.\w{2,6}", re.VERBOSE)


@dataclass
class SearchIntent:
    score: float
    verb_match: str
    has_file_path: bool
    is_simple_noun: bool

    @property
    def should_search(self) -> bool:
        return self.score >= SEARCH_INTENT_THRESHOLD


@dataclass
class SearchHit:
    source: str
    file: str
    line_number: int
    line: str


@dataclass
class SearchResult:
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    tier: str = "fallthrough"
    reason: str = ""

    @property
    def hit_count(self) -> int:
        return len(self.hits)


class SearchIntentClassifier:
    """Classifies whether a prompt is search-shaped.

    Scoring:
      +60  matched a search verb
      +20  contains a file path pattern
      +15  short noun phrase (< 100 chars, no reasoning markers)
      -40  contains reasoning markers
      -20  multi-sentence question
    """

    def classify(self, prompt: str) -> SearchIntent:
        text = prompt.strip().lower()

        has_reasoning = any(m in text for m in _REASONING_MARKERS)

        verb_match = ""
        for verb in _SEARCH_VERBS:
            if verb in text:
                verb_match = verb
                break

        has_file_path = bool(_FILE_PATH_PATTERN.search(text))

        sentence_count = text.count("?") + text.count(".") + text.count("!")
        is_simple_noun = (
            len(text) < 100
            and sentence_count <= 1
            and not has_reasoning
        )

        score = 0.0
        if verb_match:
            score += 60
        if has_file_path:
            score += 20
        if is_simple_noun:
            score += 15
        if has_reasoning:
            score -= 40
        if sentence_count > 2:
            score -= 20

        score = max(0.0, min(100.0, score))

        return SearchIntent(
            score=score,
            verb_match=verb_match,
            has_file_path=has_file_path,
            is_simple_noun=is_simple_noun,
        )


class ProgrammaticSearchTier:
    """Executes ripgrep against registered sources and returns a SearchResult."""

    def __init__(self, sources: Optional[dict[str, str]] = None):
        self._sources = sources or {}
        self._classifier = SearchIntentClassifier()

    _TRAILING_FILLER = re.compile(
        r"\s*(configured|located|stored|defined|used|called|named|path|"
        r"placed|kept|saved|set up|setup|in (?:the )?(?:codebase|vault|context|repo|project))"
        r"?\??\s*$",
        re.IGNORECASE,
    )
    _STOP_WORDS = frozenset(["is", "are", "the", "a", "an", "for", "to", "of", "in", "at"])

    def _extract_keywords(self, prompt: str) -> str:
        text = prompt.strip()
        for verb in sorted(_SEARCH_VERBS, key=len, reverse=True):
            pattern = re.compile(re.escape(verb) + r"\s*", re.IGNORECASE)
            text = pattern.sub("", text, count=1).strip()

        text = self._TRAILING_FILLER.sub("", text).strip()
        text = re.sub(r"^(the|a|an)\s+", "", text, flags=re.IGNORECASE).strip()

        words = [w for w in text.split() if w.lower() not in self._STOP_WORDS]
        if len(words) > 3:
            text = " ".join(words[:3])

        return text or prompt.strip()

    def _run_ripgrep(self, keyword: str, path: str, source_name: str) -> list[SearchHit]:
        try:
            result = subprocess.run(
                [
                    "rg",
                    "--no-heading",
                    "--line-number",
                    "--max-count", "5",
                    "--max-filesize", "500K",
                    "--type-add", "docs:*.{md,yaml,yml,json,txt,toml}",
                    "--type", "docs",
                    "-i",
                    "--",
                    keyword,
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=RIPGREP_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

        hits: list[SearchHit] = []
        base = Path(path)
        for line in result.stdout.splitlines()[:RIPGREP_MAX_MATCHES]:
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path, lineno_str, content = parts[0], parts[1], parts[2]
            try:
                lineno = int(lineno_str)
            except ValueError:
                continue
            try:
                rel = Path(file_path).relative_to(base)
            except ValueError:
                rel = Path(file_path)
            hits.append(SearchHit(
                source=source_name,
                file=str(rel),
                line_number=lineno,
                line=content.strip()[:200],
            ))
        return hits

    def search(self, prompt: str) -> SearchResult:
        intent = self._classifier.classify(prompt)

        if not intent.should_search:
            return SearchResult(
                query=prompt,
                tier="fallthrough",
                reason=f"low search intent score ({intent.score:.0f}/100)",
            )

        keyword = self._extract_keywords(prompt)
        if len(keyword) < 2:
            return SearchResult(
                query=prompt,
                tier="fallthrough",
                reason="keyword too short after stripping verbs",
            )

        available = {
            name: path for name, path in self._sources.items()
            if Path(path).exists()
        }
        if not available:
            return SearchResult(
                query=prompt,
                tier="fallthrough",
                reason="no search sources available on disk",
            )

        all_hits: list[SearchHit] = []
        for source_name, source_path in available.items():
            all_hits.extend(self._run_ripgrep(keyword, source_path, source_name))
            if len(all_hits) >= RIPGREP_MAX_MATCHES:
                break

        if len(all_hits) == 0:
            return SearchResult(
                query=prompt,
                hits=[],
                tier="fallthrough",
                reason="zero results — escalating to LLM",
            )

        if len(all_hits) > MAX_HITS_ZERO_INFERENCE:
            return SearchResult(
                query=prompt,
                hits=all_hits[:MAX_HITS_ZERO_INFERENCE],
                tier="fallthrough",
                reason=f"too many results ({len(all_hits)}) — LLM synthesizes with context",
            )

        return SearchResult(
            query=prompt,
            hits=all_hits,
            tier="zero-inference",
            reason=f"search intent ({intent.score:.0f}/100); {len(all_hits)} hits for '{keyword}'",
        )


_search_tier: Optional[ProgrammaticSearchTier] = None


def get_search_tier(sources: Optional[dict[str, str]] = None) -> ProgrammaticSearchTier:
    global _search_tier
    if _search_tier is None:
        _search_tier = ProgrammaticSearchTier(sources=sources)
    return _search_tier
