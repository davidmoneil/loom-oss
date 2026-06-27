"""Adaptive content processor — detects content type and applies the best
compression technique to minimize token usage.

The processor classifies a blob of content (directory listing, code, API JSON,
log output, search results, prose, git output, config) and applies a tailored
compression strategy that preserves the information a downstream model actually
needs (schema/keys, errors/stack traces, code change lines, status signals,
high-entropy identifiers) while dropping bulk.

Prose compression is purely extractive (first sentence per paragraph) — no
network calls or external model dependencies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# --- Compiled regex patterns ---

RE_LS_LINE = re.compile(
    r'^[dlcbps-][rwxsStT-]{9}\s+'
    r'\d+\s+'
    r'\S+\s+\S+\s+'
    r'[\d,]+\s+'
    r'\w+\s+\d+\s+[\d:]+\s+'
    r'(.+)$',
    re.MULTILINE,
)
RE_LS_TOTAL = re.compile(r'^total\s+\d+$', re.MULTILINE)

RE_ISO_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}')
RE_SYSLOG_TIMESTAMP = re.compile(r'^[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}')

# Log template extraction — specificity order matters.
RE_TPL_UUID = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)
RE_TPL_ISO_TS = re.compile(
    r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?'
)
RE_TPL_IP_PORT = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}')
RE_TPL_IP = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}')
RE_TPL_PATH = re.compile(r'(?:/[\w._-]+){2,}(?:\.\w+)?')
RE_TPL_HEX = re.compile(r'\b0x[0-9a-fA-F]{2,}\b|(?<![.\w])[0-9a-fA-F]{8,}(?![.\w])')
RE_TPL_NUM = re.compile(r'\b\d{2,}\b')

_TPL_PIPELINE: list[tuple[re.Pattern, str]] = [
    (RE_TPL_UUID, '{UUID}'),
    (RE_TPL_ISO_TS, '{TS}'),
    (RE_TPL_IP_PORT, '{IP}:{PORT}'),
    (RE_TPL_IP, '{IP}'),
    (RE_TPL_PATH, '{PATH}'),
    (RE_TPL_HEX, '{HEX}'),
    (RE_TPL_NUM, '{N}'),
]

RE_LOG_LEVEL = re.compile(r'\b(DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\b')
LOG_LEVEL_SCORES = {
    "ERROR": 1.0, "CRITICAL": 1.0, "FATAL": 1.0,
    "WARN": 0.5, "WARNING": 0.5,
    "INFO": 0.1,
    "DEBUG": 0.05,
}

RE_PYTHON_TRACEBACK = re.compile(
    r'^\s*(?:Traceback \(most recent call last\)|File ".+", line \d+)'
)
RE_JS_AT_FRAME = re.compile(r'^\s*at .+\(.+:\d+:\d+\)')
RE_JAVA_AT_FRAME = re.compile(r'^\s*at [\w.$]+\(')
RE_RUST_ERROR_FRAME = re.compile(r'^\s*--> .+:\d+:\d+')
RE_GO_FRAME = re.compile(r'^\s*\d+:\s+0x[0-9a-fA-F]+')

RE_SUMMARY_LINE = re.compile(
    r'^(?:={3,}|'
    r'-{3,}|'
    r'\d+\s+(?:passed|failed|skipped|error)|'
    r'(?:Tests?|Suites?):?\s+\d+|'
    r'(?:TOTAL|Total|Summary)|'
    r'(?:Build|Compile|Test).*(?:succeeded|failed|complete))'
)

RE_CLASS_DEF = re.compile(r'^(\s*)(class\s+\w+[^:]*:)', re.MULTILINE)
RE_FUNC_DEF = re.compile(r'^(\s*)((?:async\s+)?def\s+\w+\s*\([^)]*\)[^:]*:)', re.MULTILINE)
RE_DECORATOR = re.compile(r'^(\s*)@\w+', re.MULTILINE)
RE_IMPORT = re.compile(r'^(?:import\s|from\s)', re.MULTILINE)
RE_DOCSTRING_START = re.compile(r'^\s*("""|\'\'\')(.*)$')

RE_GIT_DIFF_FILE = re.compile(r'^diff --git a/(.+?) b/(.+)$', re.MULTILINE)
RE_GIT_DIFF_COMBINED = re.compile(r'^diff --(?:combined|cc) (.+)$', re.MULTILINE)
RE_GIT_STAT_LINE = re.compile(r'^\s*(\d+)\s+insertions?.*?(\d+)\s+deletions?', re.MULTILINE)
RE_GIT_DIFF_HUNK = re.compile(r'^(?:@@@ .+ @@@|@@ .+ @@)(.*)', re.MULTILINE)
RE_GIT_LOG_COMMIT = re.compile(r'^commit\s+[0-9a-f]{40}', re.MULTILINE)
RE_GIT_LOG_ONELINE = re.compile(r'^[0-9a-f]{7,40}\s+', re.MULTILINE)

RE_DIFF_PRIORITY = re.compile(
    r'\b(?:error|exception|fail(?:ed|ure)?|fatal|critical|crash|panic|'
    r'security|auth|password|secret|token|'
    r'todo|fixme|hack|bug|fix)\b',
    re.IGNORECASE,
)

# --- Schema-preserving JSON compression constants ---
JSON_SHORT_VALUE_THRESHOLD = 20
JSON_MAX_NUMBER_DIGITS = 10
JSON_MAX_ARRAY_ITEMS = 3
JSON_MAX_DEPTH = 3
JSON_DEEP_NEST_TRUNCATE = 100

# --- Entropy preservation ---
ENTROPY_MIN_LENGTH = 8
ENTROPY_THRESHOLD = 0.85


def _compute_entropy(text: str) -> float:
    """Normalized Shannon entropy in [0.0, 1.0]; 1.0 = maximum randomness.

    UUIDs, hashes, and API keys typically score > 0.85; natural language and
    repeated text score lower.
    """
    if not text or len(text) < 2:
        return 0.0
    freq = Counter(text)
    total = len(text)
    entropy = -sum(
        (count / total) * math.log2(count / total)
        for count in freq.values()
        if count > 0
    )
    max_entropy = math.log2(len(freq)) if len(freq) > 1 else 1.0
    return entropy / max_entropy if max_entropy > 0 else 0.0


RE_IDENTIFIER_PATTERN = re.compile(
    r'^(?:'
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    r'|[0-9a-fA-F]{16,}'
    r'|[A-Za-z0-9+/=]{20,}'
    r'|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'
    r'|sk-[A-Za-z0-9]{20,}'
    r'|[a-z]{2,4}_[A-Za-z0-9]{16,}'
    r')$'
)


def _is_high_entropy(text: str) -> bool:
    """True if the string is an identifier (UUID, hash, API key, ...) that must
    survive compression.

    Uses structural pattern matching plus a Shannon-entropy fallback guarded by
    structural checks (no whitespace, length, digit presence, alnum density) to
    avoid false positives on natural language.
    """
    if len(text) < ENTROPY_MIN_LENGTH:
        return False

    if RE_IDENTIFIER_PATTERN.match(text):
        return True

    if " " in text or "\n" in text:
        return False
    if len(text) < 16:
        return False
    if not any(c.isdigit() for c in text):
        return False
    alnum_count = sum(1 for c in text if c.isalnum())
    if alnum_count / len(text) < 0.85:
        return False

    return _compute_entropy(text) >= ENTROPY_THRESHOLD


# Status signals — decision-critical markers that must survive compression.
RE_STATUS_SIGNALS = [
    re.compile(r'\b(\d+)\s+(?:tasks?|items?|results?|records?|files?)\s+(?:found|returned|matched|queued|eligible)', re.IGNORECASE),
    re.compile(r'\b(?:exit[_ ]?code|status)[:\s]+(\d+)', re.IGNORECASE),
    re.compile(r'\b(?:success(?:fully)?|completed?|finished|done|closed|written|saved|created|deployed|claimed)\b', re.IGNORECASE),
    re.compile(r'\b(?:fail(?:ed|ure)?|error|refused|denied|timeout|not found|no (?:such|tasks?|results?)|0 (?:tasks?|items?|results?))\b', re.IGNORECASE),
    re.compile(r'(?:written|saved|output|report)\s+(?:to|at)\s+[`"]?(/\S+|[.\w/-]+\.\w+)', re.IGNORECASE),
    re.compile(r'\b(?:skipped?|parked|deferred|waiting|blocked)\b', re.IGNORECASE),
]

RE_FILLER_PHRASES = re.compile(
    r'\b(?:'
    r'(?:it (?:is|was|should be) (?:important|worth) (?:to )?(?:note|noting|mentioning) that)|'
    r'(?:please (?:note|be aware) that)|'
    r'(?:as (?:you can see|mentioned (?:above|below|earlier|previously)))|'
    r'(?:in order to)|'
    r'(?:the fact that)|'
    r'(?:it is (?:also )?(?:worth|important) (?:to )?(?:note|mention))|'
    r'(?:at this point in time)|'
    r'(?:for the purpose of)|'
    r'(?:in the event that)|'
    r'(?:with respect to)|'
    r'(?:it should be noted that)|'
    r'(?:as a matter of fact)|'
    r'(?:at the end of the day)'
    r')\s*',
    re.IGNORECASE,
)

RE_GREP_LINE = re.compile(r'^([^\s:]+\.\w+):(\d+)[:-](.*)$', re.MULTILINE)
RE_FIND_OUTPUT = re.compile(r'^\.?/[\w./-]+\.\w+$', re.MULTILINE)

RE_REDUNDANT_WHITESPACE = re.compile(r'[ \t]{2,}')
RE_EMPTY_LINES = re.compile(r'\n{3,}')
RE_FILLER_WORDS = re.compile(
    r'\b(?:basically|actually|really|very|quite|rather|somewhat|'
    r'simply|just|certainly|definitely|obviously|clearly|'
    r'essentially|practically|virtually|literally|'
    r'furthermore|moreover|however|therefore|consequently|'
    r'nevertheless|nonetheless|accordingly|subsequently)\b\s*',
    re.IGNORECASE,
)

CODE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h"}
CONFIG_EXTENSIONS = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf"}

ESTIMATED_SAVINGS = {
    "dir_listing": 0.90,
    "code": 0.60,
    "api_json": 0.75,
    "log_output": 0.80,
    "search_output": 0.85,
    "prose": 0.70,
    "git_output": 0.55,
    "config": 0.30,
}

TECHNIQUES = {
    "dir_listing": "names_only",
    "code": "signatures",
    "api_json": "schema_preserving",
    "log_output": "dedup",
    "search_output": "group_by_file",
    "prose": "paragraph",
    "git_output": "structural",
    "config": "structural",
}

MIN_TOKENS_TO_COMPRESS = 50


def _extract_log_template(line: str) -> str:
    """Replace the variable parts of a log line with placeholders so lines that
    differ only in timestamps/IPs/UUIDs/paths/numbers group together."""
    result = line
    for pattern, placeholder in _TPL_PIPELINE:
        result = pattern.sub(placeholder, result)
    return result


@dataclass
class CompressedVariant:
    technique: str
    content_type: str
    original_tokens: int
    compressed_tokens: int
    text: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    content_hash: str = ""

    @property
    def savings_pct(self) -> float:
        if self.original_tokens == 0:
            return 0.0
        return 1.0 - (self.compressed_tokens / self.original_tokens)


@dataclass
class ProcessingPlan:
    content_type: str
    technique: str
    requires_llm: bool
    estimated_savings: float


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _extract_extension(source_hint: str) -> str:
    """Pull the file extension from a source hint like 'Read:/path/to/file.py'."""
    if ":" in source_hint:
        path_part = source_hint.split(":", 1)[1].strip()
    else:
        path_part = source_hint.strip()
    path_part = path_part.split()[0] if " " in path_part else path_part
    dot_idx = path_part.rfind(".")
    if dot_idx >= 0:
        return path_part[dot_idx:].lower()
    return ""


class ContentProcessor:
    """Evaluates content and applies adaptive compression."""

    def __init__(self, config: Any = None) -> None:
        self._config = config

    def detect_content_type(self, content: str, source_hint: str = "") -> str:
        hint_lower = source_hint.lower()

        # 1. Git output (source_hint first, then content).
        if "git diff" in hint_lower or "git log" in hint_lower:
            return "git_output"
        if (RE_GIT_DIFF_FILE.search(content) or RE_GIT_DIFF_COMBINED.search(content)
                or RE_GIT_LOG_COMMIT.search(content)):
            return "git_output"

        # 2. API JSON from curl.
        if "curl" in hint_lower and content.lstrip().startswith("{"):
            return "api_json"

        # 3. Directory listing.
        if "ls " in hint_lower or "find " in hint_lower:
            return "dir_listing"
        if RE_LS_TOTAL.search(content) and RE_LS_LINE.search(content):
            return "dir_listing"

        # 4. Log output: >30% of lines have timestamps.
        lines = content.splitlines()
        if len(lines) > 3:
            ts_count = sum(
                1 for line in lines
                if RE_ISO_TIMESTAMP.search(line) or RE_SYSLOG_TIMESTAMP.search(line)
            )
            if ts_count / len(lines) > 0.3:
                return "log_output"

        # 5. Code files by extension.
        ext = _extract_extension(source_hint)
        if ext in CODE_EXTENSIONS:
            return "code"

        # 6. Config files by extension.
        if ext in CONFIG_EXTENSIONS:
            return "config"

        # 7. Code by content (class/def patterns without timestamps).
        if RE_CLASS_DEF.search(content) or RE_FUNC_DEF.search(content):
            code_markers = len(RE_FUNC_DEF.findall(content)) + len(RE_CLASS_DEF.findall(content))
            if code_markers >= 2:
                return "code"

        # 8. JSON content (not from curl but still JSON).
        stripped = content.strip()
        if (stripped.startswith("{") or stripped.startswith("[")) and len(stripped) > 50:
            try:
                json.loads(stripped)
                return "api_json"
            except (json.JSONDecodeError, ValueError):
                pass

        # 9. Search output (grep/rg/ag results — find is caught at step 3).
        search_hints = ("grep", "rg ", "rg\t", "ag ", "search")
        if any(h in hint_lower for h in search_hints):
            return "search_output"
        if lines and len(lines) > 3:
            grep_count = sum(1 for line in lines if RE_GREP_LINE.match(line))
            if grep_count / len(lines) > 0.5:
                return "search_output"
            find_count = sum(1 for line in lines if RE_FIND_OUTPUT.match(line))
            if find_count / len(lines) > 0.5:
                return "search_output"

        # 10. Prose: paragraphs with long lines, no dominant code markers.
        if lines:
            long_lines = sum(1 for line in lines if len(line) > 60)
            if long_lines / len(lines) > 0.3:
                return "prose"

        return "prose"

    def evaluate(self, content: str, source_hint: str = "",
                 token_count: int = 0) -> Optional[ProcessingPlan]:
        """Decide a compression technique. None if not worth compressing."""
        tokens = token_count or _estimate_tokens(content)
        if tokens < MIN_TOKENS_TO_COMPRESS:
            return None

        content_type = self.detect_content_type(content, source_hint)
        technique = TECHNIQUES[content_type]
        requires_llm = technique == "paragraph"
        estimated_savings = ESTIMATED_SAVINGS[content_type]

        return ProcessingPlan(
            content_type=content_type,
            technique=technique,
            requires_llm=requires_llm,
            estimated_savings=estimated_savings,
        )

    def compress(self, content: str, plan: ProcessingPlan) -> CompressedVariant:
        """Execute compression. Never raises — returns original on failure."""
        original_tokens = _estimate_tokens(content)

        try:
            dispatch = {
                "names_only": self._compress_dir_listing,
                "signatures": self._compress_code,
                "schema_preserving": self._compress_api_json,
                "dedup": self._compress_log_output,
                "group_by_file": self._compress_search_results,
                "structural": (
                    self._compress_git_output if plan.content_type == "git_output"
                    else self._compress_config
                ),
            }

            if plan.technique == "paragraph":
                compressed_text = self._compress_prose(content)
            else:
                handler = dispatch.get(plan.technique)
                compressed_text = handler(content) if handler else content
        except Exception:
            logger.exception(
                "Compression failed for %s/%s, returning original",
                plan.content_type, plan.technique,
            )
            compressed_text = content

        status_signals = self._extract_status_signals(content)
        if status_signals:
            signal_block = "\n[Status: " + " | ".join(status_signals) + "]"
            if signal_block not in compressed_text:
                compressed_text = compressed_text.rstrip() + "\n" + signal_block

        compressed_tokens = _estimate_tokens(compressed_text)
        return CompressedVariant(
            technique=plan.technique,
            content_type=plan.content_type,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            text=compressed_text,
            content_hash=_content_hash(content),
        )

    @staticmethod
    def _extract_status_signals(content: str) -> list[str]:
        """Extract decision-critical status markers (exit codes, completion
        markers, counts, output paths) so they survive compression."""
        signals = []
        seen = set()
        for pattern in RE_STATUS_SIGNALS:
            for match in pattern.finditer(content):
                text = match.group(0).strip()
                if text.lower() not in seen and len(text) < 200:
                    signals.append(text)
                    seen.add(text.lower())
        return signals[:10]

    def compress_light(self, content: str) -> str:
        """Light compression: remove filler words/phrases, collapse whitespace.

        ~25% token savings; preserves all facts, numbers, paths, and structure.
        """
        text = RE_FILLER_PHRASES.sub("", content)
        text = RE_FILLER_WORDS.sub("", text)
        text = RE_REDUNDANT_WHITESPACE.sub(" ", text)
        text = RE_EMPTY_LINES.sub("\n\n", text)
        return text.strip()

    def compress_graduated(self, content: str, age_ratio: float,
                           source_hint: str = "") -> tuple[str, str]:
        """Graduated compression based on content age within a session.

        age_ratio: 0.0 = most recent, 1.0 = oldest. Returns (text, tier) where
        tier is one of: full (0.0-0.3), light (0.3-0.5), medium (0.5-0.7),
        heavy (0.7-1.0).
        """
        if age_ratio < 0.3:
            return content, "full"

        if age_ratio < 0.5:
            return self.compress_light(content), "light"

        if age_ratio < 0.7:
            plan = self.evaluate(content, source_hint, _estimate_tokens(content))
            if plan is not None:
                variant = self.compress(content, plan)
                return variant.text, "medium"
            return self.compress_light(content), "light"

        signals = self._extract_status_signals(content)
        if signals:
            return "[Status: " + " | ".join(signals[:5]) + "]", "heavy"
        return f"[{_estimate_tokens(content)} tokens evicted]", "heavy"

    # --- Compression implementations ---

    def _compress_dir_listing(self, content: str) -> str:
        names = []
        for match in RE_LS_LINE.finditer(content):
            name = match.group(1).strip()
            full_line = content[content.rfind("\n", 0, match.start()) + 1:match.end()]
            if full_line.startswith("d"):
                name = name.rstrip("/") + "/"
            names.append(name)

        if names:
            return "\n".join(names)

        lines = [
            line.strip() for line in content.splitlines()
            if line.strip() and not line.strip().startswith("total ")
        ]
        return "\n".join(lines) if lines else content

    def _compress_search_results(self, content: str) -> str:
        """Group grep/rg/find output per file: keep the first 2 matches per
        file then a remainder count. Returns the original if it doesn't parse."""
        from collections import OrderedDict

        lines = content.splitlines()
        grouped: "OrderedDict[str, list[tuple[str, str]]]" = OrderedDict()
        unparsed = 0

        for line in lines:
            if not line.strip():
                continue
            m = RE_GREP_LINE.match(line)
            if m:
                filepath, lineno, match_content = m.group(1), m.group(2), m.group(3)
                grouped.setdefault(filepath, []).append((lineno, match_content))
                continue
            if RE_FIND_OUTPUT.match(line.strip()):
                grouped.setdefault(line.strip(), [])
                continue
            unparsed += 1

        total = len([l for l in lines if l.strip()])
        if total == 0 or unparsed / total > 0.5:
            return content

        max_shown = 2
        result: list[str] = []
        for filepath, matches in grouped.items():
            if not matches:
                result.append(filepath)
                continue
            result.append(f"{filepath} ({len(matches)} results):")
            for lineno, match_content in matches[:max_shown]:
                result.append(f"  :{lineno}: {match_content}")
            remaining = len(matches) - max_shown
            if remaining > 0:
                result.append(f"  ... {remaining} more result{'s' if remaining != 1 else ''}")

        return "\n".join(result)

    def _compress_code(self, content: str) -> str:
        lines = content.splitlines()
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.rstrip()

            if RE_IMPORT.match(stripped):
                result.append(stripped)
                i += 1
                continue

            if RE_DECORATOR.match(stripped):
                while i < len(lines) and RE_DECORATOR.match(lines[i].rstrip()):
                    result.append(lines[i].rstrip())
                    i += 1
                if i < len(lines):
                    line = lines[i]
                    stripped = line.rstrip()
                else:
                    continue

            if RE_CLASS_DEF.match(stripped):
                result.append(stripped)
                i += 1
                if i < len(lines):
                    ds_match = RE_DOCSTRING_START.match(lines[i])
                    if ds_match:
                        quote = ds_match.group(1)
                        ds_text = ds_match.group(2)
                        if ds_text.rstrip().endswith(quote):
                            result.append(lines[i].rstrip())
                            i += 1
                        else:
                            result.append(lines[i].rstrip())
                            i += 1
                            while i < len(lines) and quote not in lines[i]:
                                i += 1
                            if i < len(lines):
                                i += 1
                continue

            if RE_FUNC_DEF.match(stripped):
                result.append(stripped.rstrip(":") + ": ...")
                i += 1
                if i < len(lines):
                    ds_match = RE_DOCSTRING_START.match(lines[i])
                    if ds_match:
                        quote = ds_match.group(1)
                        ds_text = ds_match.group(2)
                        if ds_text.rstrip().endswith(quote):
                            i += 1
                        else:
                            i += 1
                            while i < len(lines) and quote not in lines[i]:
                                i += 1
                            if i < len(lines):
                                i += 1
                if i < len(lines):
                    indent_level = len(stripped) - len(stripped.lstrip())
                    while i < len(lines):
                        next_line = lines[i]
                        if next_line.strip() == "":
                            i += 1
                            continue
                        next_indent = len(next_line) - len(next_line.lstrip())
                        if next_indent <= indent_level:
                            break
                        for token in re.split(r'[\s=:,\[\]{}()"\']+', next_line):
                            if _is_high_entropy(token):
                                result.append(next_line.rstrip())
                                break
                        i += 1
                continue

            if stripped == "" and result and result[-1] != "":
                result.append("")

            i += 1

        while result and result[-1] == "":
            result.pop()

        return "\n".join(result)

    def _compress_api_json(self, content: str) -> str:
        """Schema-preserving JSON compression: keep ALL keys visible while
        compressing long string values, deep nesting, and long arrays."""
        stripped = content.strip()
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return content

        compressed = self._compress_value(data, depth=0)
        return json.dumps(compressed, separators=(",", ":"))

    def _compress_value(self, data, depth: int = 0):
        if isinstance(data, dict):
            if depth > JSON_MAX_DEPTH:
                s = json.dumps(data, separators=(",", ":"))
                if len(s) > JSON_DEEP_NEST_TRUNCATE:
                    return s[:JSON_DEEP_NEST_TRUNCATE] + "..."
                return s
            return {
                key: self._compress_value(val, depth + 1)
                for key, val in data.items()
            }

        if isinstance(data, list):
            if len(data) == 0:
                return []
            kept = [
                self._compress_value(item, depth + 1)
                for item in data[:JSON_MAX_ARRAY_ITEMS]
            ]
            remaining = len(data) - JSON_MAX_ARRAY_ITEMS
            if remaining > 0:
                kept.append({"_remaining": remaining})
            return kept

        if isinstance(data, str):
            if _is_high_entropy(data):
                return data
            if len(data) <= JSON_SHORT_VALUE_THRESHOLD:
                return data
            return data[:JSON_SHORT_VALUE_THRESHOLD] + "..."

        if isinstance(data, bool):
            return data

        if isinstance(data, (int, float)):
            s = str(data)
            if len(s) <= JSON_MAX_NUMBER_DIGITS:
                return data
            return s[:JSON_MAX_NUMBER_DIGITS] + "..."

        return data

    # --- Log compression config ---
    LOG_MAX_ERRORS = 10
    LOG_MAX_WARNINGS = 5
    LOG_MAX_STACK_TRACES = 3
    LOG_STACK_TRACE_MAX_LINES = 20
    LOG_CONTEXT_LINES = 3
    LOG_MAX_TOTAL_LINES = 100
    LOG_MIN_LINES_TO_COMPRESS = 20
    LOG_DEDUPE_WARNINGS = True

    def _compress_log_output(self, content: str) -> str:
        """Compress log output preserving errors, stack traces, and warnings,
        deduplicating by template and emitting an omission summary."""
        lines = content.splitlines()
        if not lines or len(lines) < self.LOG_MIN_LINES_TO_COMPRESS:
            return content

        classified = self._classify_log_lines(lines)
        selected_indices = self._select_log_lines(classified, lines)

        with_context = set(selected_indices)
        for idx in selected_indices:
            for i in range(max(0, idx - self.LOG_CONTEXT_LINES),
                           min(len(lines), idx + self.LOG_CONTEXT_LINES + 1)):
                with_context.add(i)

        ordered = sorted(with_context)
        if len(ordered) > self.LOG_MAX_TOTAL_LINES:
            scored = [(i, classified[i]["score"]) for i in ordered]
            scored.sort(key=lambda x: x[1], reverse=True)
            top_indices = {i for i, _ in scored[:self.LOG_MAX_TOTAL_LINES]}
            ordered = sorted(top_indices)

        result: list[str] = []
        first_occurrence: dict[str, tuple[str, int]] = {}
        template_order: list[str] = []
        for i in ordered:
            line = lines[i]
            template = _extract_log_template(line)
            if template in first_occurrence:
                example, count = first_occurrence[template]
                first_occurrence[template] = (example, count + 1)
            else:
                first_occurrence[template] = (line, 1)
                template_order.append(template)

        for template in template_order:
            example, count = first_occurrence[template]
            if count > 1:
                result.append(f"{example} ({count} occurrences)")
            else:
                result.append(example)

        omitted = len(lines) - len(ordered)
        if omitted > 0:
            error_count = sum(1 for c in classified if c["level"] in ("ERROR", "CRITICAL", "FATAL"))
            warn_count = sum(1 for c in classified if c["level"] in ("WARN", "WARNING"))
            info_count = sum(1 for c in classified if c["level"] == "INFO")
            parts = []
            if error_count:
                parts.append(f"{error_count} ERROR")
            if warn_count:
                parts.append(f"{warn_count} WARN")
            if info_count:
                parts.append(f"{info_count} INFO")
            if parts:
                result.append(f"[{omitted} lines omitted: {', '.join(parts)}]")

        return "\n".join(result)

    def _classify_log_lines(self, lines: list[str]) -> list[dict]:
        classified = []
        in_trace = False
        trace_flavor: Optional[str] = None
        trace_line_count = 0

        for line in lines:
            entry = {
                "level": "UNKNOWN",
                "is_stack_trace": False,
                "is_summary": False,
                "score": 0.0,
            }

            level_match = RE_LOG_LEVEL.search(line)
            if level_match:
                entry["level"] = level_match.group(1).upper()

            if RE_SUMMARY_LINE.match(line.strip()):
                entry["is_summary"] = True

            if in_trace:
                if trace_line_count >= self.LOG_STACK_TRACE_MAX_LINES:
                    in_trace = False
                    trace_flavor = None
                    trace_line_count = 0
                elif self._trace_terminates(trace_flavor, line):
                    in_trace = False
                    trace_flavor = None
                    trace_line_count = 0
                    new_flavor = self._detect_trace_start(line)
                    if new_flavor:
                        in_trace = True
                        trace_flavor = new_flavor
                        trace_line_count = 1
                        entry["is_stack_trace"] = True
                else:
                    entry["is_stack_trace"] = True
                    trace_line_count += 1
            else:
                flavor = self._detect_trace_start(line)
                if flavor:
                    in_trace = True
                    trace_flavor = flavor
                    trace_line_count = 1
                    entry["is_stack_trace"] = True

            level_score = LOG_LEVEL_SCORES.get(entry["level"], 0.1)
            stack_boost = 0.3 if entry["is_stack_trace"] else 0.0
            summary_boost = 0.4 if entry["is_summary"] else 0.0
            entry["score"] = min(1.0, level_score + stack_boost + summary_boost)

            classified.append(entry)

        return classified

    def _select_log_lines(self, classified: list[dict],
                          lines: list[str]) -> set[int]:
        selected: set[int] = set()

        errors = [i for i, c in enumerate(classified)
                  if c["level"] in ("ERROR", "CRITICAL", "FATAL")]
        warnings = [i for i, c in enumerate(classified)
                    if c["level"] in ("WARN", "WARNING")]
        summaries = [i for i, c in enumerate(classified) if c["is_summary"]]

        trace_groups: list[list[int]] = []
        current_group: list[int] = []
        for i, c in enumerate(classified):
            if c["is_stack_trace"]:
                current_group.append(i)
            elif current_group:
                trace_groups.append(current_group)
                current_group = []
        if current_group:
            trace_groups.append(current_group)

        if errors:
            selected.add(errors[0])
            selected.add(errors[-1])
            if len(errors) > self.LOG_MAX_ERRORS:
                middle = errors[1:-1]
                middle_scored = sorted(
                    middle, key=lambda i: classified[i]["score"], reverse=True
                )
                for idx in middle_scored[:self.LOG_MAX_ERRORS - 2]:
                    selected.add(idx)
            else:
                selected.update(errors)

        for group in trace_groups[:self.LOG_MAX_STACK_TRACES]:
            for idx in group[:self.LOG_STACK_TRACE_MAX_LINES]:
                selected.add(idx)

        if self.LOG_DEDUPE_WARNINGS and warnings:
            seen_keys: set[str] = set()
            deduped: list[int] = []
            for idx in warnings:
                key = _extract_log_template(lines[idx])
                if key not in seen_keys:
                    seen_keys.add(key)
                    deduped.append(idx)
            warnings = deduped

        for idx in warnings[:self.LOG_MAX_WARNINGS]:
            selected.add(idx)

        selected.update(summaries)

        for i, line in enumerate(lines):
            if i not in selected:
                for token in re.split(r'[\s=:,\[\]{}()"\']+', line):
                    if _is_high_entropy(token):
                        selected.add(i)
                        break

        return selected

    @staticmethod
    def _detect_trace_start(line: str) -> Optional[str]:
        stripped = line.strip()
        if RE_PYTHON_TRACEBACK.match(stripped):
            return "python"
        if RE_JS_AT_FRAME.match(stripped):
            return "js"
        if RE_JAVA_AT_FRAME.match(stripped):
            return "java"
        if RE_RUST_ERROR_FRAME.match(stripped):
            return "rust"
        if RE_GO_FRAME.match(stripped):
            return "go"
        return None

    @staticmethod
    def _trace_terminates(flavor: Optional[str], line: str) -> bool:
        if flavor is None:
            return True

        stripped = line.strip()

        if flavor == "python":
            is_continuation = (
                line.startswith((" ", "\t")) or
                line == "" or
                stripped.startswith("Traceback") or
                stripped.startswith("File ") or
                stripped.startswith("During handling") or
                stripped.startswith("The above exception")
            )
            if is_continuation:
                return False
            if stripped and stripped[0].isupper() and ":" in stripped:
                return False
            return True

        if flavor in ("js", "java"):
            return not stripped.startswith("at ") and stripped != ""

        if flavor == "rust":
            return not stripped.startswith("--> ") and stripped != ""

        if flavor == "go":
            return not (stripped and stripped[0].isdigit()) and stripped != ""

        return True

    def _compress_git_output(self, content: str) -> str:
        if RE_GIT_DIFF_FILE.search(content) or RE_GIT_DIFF_COMBINED.search(content):
            return self._compress_git_diff(content)
        if RE_GIT_LOG_COMMIT.search(content) or RE_GIT_LOG_ONELINE.search(content):
            return self._compress_git_log(content)
        return content

    # --- Diff compression config ---
    DIFF_MAX_FILES = 20
    DIFF_MAX_HUNKS_PER_FILE = 10
    DIFF_MAX_CONTEXT_LINES = 2
    DIFF_MIN_LINES_TO_COMPRESS = 30

    def _compress_git_diff(self, content: str) -> str:
        """Compress unified diffs preserving actual code changes: cap files and
        per-file hunks, trim context, and emit a summary footer."""
        lines = content.splitlines()
        if len(lines) < self.DIFF_MIN_LINES_TO_COMPRESS:
            return content

        pre_diff, diff_files = self._parse_unified_diff(lines)
        if not diff_files:
            return content

        for df in diff_files:
            for hunk in df["hunks"]:
                hunk["score"] = self._score_hunk(hunk)

        if len(diff_files) > self.DIFF_MAX_FILES:
            diff_files.sort(
                key=lambda f: sum(h["additions"] + h["deletions"] for h in f["hunks"]),
                reverse=True,
            )
            diff_files = diff_files[:self.DIFF_MAX_FILES]

        total_additions = 0
        total_deletions = 0
        hunks_removed = 0

        for df in diff_files:
            total_additions += sum(h["additions"] for h in df["hunks"])
            total_deletions += sum(h["deletions"] for h in df["hunks"])

            original_count = len(df["hunks"])
            if original_count > self.DIFF_MAX_HUNKS_PER_FILE:
                df["hunks"] = self._select_hunks(df["hunks"], self.DIFF_MAX_HUNKS_PER_FILE)
                hunks_removed += original_count - len(df["hunks"])

            df["hunks"] = [
                self._trim_hunk_context(h, self.DIFF_MAX_CONTEXT_LINES)
                for h in df["hunks"]
            ]

        out = []
        out.extend(pre_diff)
        for df in diff_files:
            out.append(df["header"])
            for marker in df.get("rename_lines", []):
                out.append(marker)
            if df.get("is_new_file"):
                out.append("new file mode 100644")
            elif df.get("is_deleted_file"):
                out.append("deleted file mode 100644")
            if df.get("is_binary"):
                out.append("Binary files differ")
                continue
            if df.get("old_file"):
                out.append(df["old_file"])
            if df.get("new_file"):
                out.append(df["new_file"])
            for hunk in df["hunks"]:
                out.append(hunk["header"])
                out.extend(hunk["lines"])

        parts = [f"{len(diff_files)} files changed",
                 f"+{total_additions} -{total_deletions} lines"]
        if hunks_removed > 0:
            parts.append(f"{hunks_removed} hunks omitted")
        out.append(f"[{', '.join(parts)}]")

        return "\n".join(out)

    @staticmethod
    def _parse_unified_diff(lines: list[str]) -> tuple[list[str], list[dict]]:
        pre_diff: list[str] = []
        files: list[dict] = []
        current_file: Optional[dict] = None
        current_hunk: Optional[dict] = None

        for line in lines:
            is_diff_header = (
                RE_GIT_DIFF_FILE.match(line) or RE_GIT_DIFF_COMBINED.match(line)
            )
            if is_diff_header:
                if current_hunk and current_file is not None:
                    current_file["hunks"].append(current_hunk)
                    current_hunk = None
                if current_file is not None:
                    files.append(current_file)
                current_file = {
                    "header": line,
                    "old_file": "",
                    "new_file": "",
                    "hunks": [],
                    "is_binary": False,
                    "is_new_file": False,
                    "is_deleted_file": False,
                    "rename_lines": [],
                }
                continue

            if current_file is None:
                pre_diff.append(line)
                continue

            if line.startswith("new file mode"):
                current_file["is_new_file"] = True
            elif line.startswith("deleted file mode"):
                current_file["is_deleted_file"] = True
            elif line.startswith(("rename ", "similarity ", "copy ", "dissimilarity ")):
                current_file["rename_lines"].append(line)
            elif re.match(r'^Binary files .+ differ$', line):
                current_file["is_binary"] = True

            if line.startswith("--- "):
                current_file["old_file"] = line
                continue
            if line.startswith("+++ "):
                current_file["new_file"] = line
                continue

            if RE_GIT_DIFF_HUNK.match(line):
                if current_hunk:
                    current_file["hunks"].append(current_hunk)
                current_hunk = {
                    "header": line,
                    "lines": [],
                    "additions": 0,
                    "deletions": 0,
                    "context_lines": 0,
                    "score": 0.0,
                }
                continue

            if current_hunk is not None:
                if line.startswith("+") and not line.startswith("+++"):
                    current_hunk["additions"] += 1
                    current_hunk["lines"].append(line)
                elif line.startswith("-") and not line.startswith("---"):
                    current_hunk["deletions"] += 1
                    current_hunk["lines"].append(line)
                elif line.startswith(" ") or line == "":
                    current_hunk["context_lines"] += 1
                    current_hunk["lines"].append(line)
                else:
                    current_hunk["lines"].append(line)

        if current_hunk and current_file is not None:
            current_file["hunks"].append(current_hunk)
        if current_file is not None:
            files.append(current_file)

        return pre_diff, files

    @staticmethod
    def _score_hunk(hunk: dict) -> float:
        change_count = hunk["additions"] + hunk["deletions"]
        score = min(0.3, change_count * 0.03)
        hunk_text = "\n".join(hunk["lines"])
        if RE_DIFF_PRIORITY.search(hunk_text):
            score += 0.3
        return min(1.0, score)

    @staticmethod
    def _select_hunks(hunks: list[dict], max_count: int) -> list[dict]:
        if len(hunks) <= max_count:
            return hunks
        first = hunks[0]
        last = hunks[-1]
        middle = hunks[1:-1]
        remaining_slots = max_count - 2
        middle_sorted = sorted(middle, key=lambda h: h["score"], reverse=True)
        kept_middle = middle_sorted[:remaining_slots]
        middle_headers = {h["header"] for h in kept_middle}
        result = [first]
        for h in hunks[1:-1]:
            if h["header"] in middle_headers:
                result.append(h)
        result.append(last)
        return result

    @staticmethod
    def _trim_hunk_context(hunk: dict, max_context: int) -> dict:
        lines = hunk["lines"]
        if not lines:
            return hunk

        change_positions = [
            i for i, line in enumerate(lines)
            if line.startswith("+") or line.startswith("-")
        ]

        if not change_positions:
            kept = lines[:max_context]
            return {
                **hunk,
                "lines": kept,
                "additions": 0,
                "deletions": 0,
                "context_lines": len(kept),
            }

        keep = set()
        for pos in change_positions:
            keep.add(pos)
            for i in range(max(0, pos - max_context), pos):
                keep.add(i)
            for i in range(pos + 1, min(len(lines), pos + max_context + 1)):
                keep.add(i)

        for i, line in enumerate(lines):
            if line.startswith("\\"):
                keep.add(i)

        new_lines = []
        additions = 0
        deletions = 0
        context_lines = 0
        for i in sorted(keep):
            line = lines[i]
            new_lines.append(line)
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
            else:
                context_lines += 1

        return {
            **hunk,
            "lines": new_lines,
            "additions": additions,
            "deletions": deletions,
            "context_lines": context_lines,
        }

    def _compress_git_log(self, content: str) -> str:
        if RE_GIT_LOG_COMMIT.search(content):
            messages = []
            lines = content.splitlines()
            i = 0
            while i < len(lines):
                if lines[i].startswith("commit "):
                    i += 1
                    while i < len(lines) and (
                        lines[i].startswith("Author:") or
                        lines[i].startswith("Date:") or
                        lines[i].startswith("Merge:") or
                        lines[i].strip() == ""
                    ):
                        i += 1
                    if i < len(lines):
                        msg = lines[i].strip()
                        if msg:
                            messages.append(msg)
                i += 1
            return "\n".join(messages) if messages else content

        if RE_GIT_LOG_ONELINE.search(content):
            result = []
            for line in content.splitlines():
                match = RE_GIT_LOG_ONELINE.match(line)
                if match:
                    result.append(line[match.end():].strip())
                elif line.strip():
                    result.append(line.strip())
            return "\n".join(result)

        return content

    def _compress_config(self, content: str) -> str:
        lines = content.splitlines()
        result = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if stripped == "":
                continue
            if stripped.startswith("-") or ":" in stripped or "=" in stripped:
                if "#" in stripped and not (stripped.count('"') % 2 or stripped.count("'") % 2):
                    comment_idx = stripped.rfind(" #")
                    if comment_idx > 0:
                        line = line[:line.rfind(" #")]
            result.append(line.rstrip())
        return "\n".join(result)

    def _compress_prose(self, content: str) -> str:
        """Extractive prose compression: first sentence of each paragraph.

        Code blocks and indented blocks are preserved verbatim.
        """
        paragraphs = re.split(r"\n\s*\n", content)
        sentences = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if para.startswith("```") or para.startswith("    "):
                sentences.append(para)
                continue
            match = re.match(r'(.+?[.!?])(?:\s|$)', para, re.DOTALL)
            if match:
                sentences.append(match.group(1).strip())
            else:
                sentences.append(para[:200])
        return "\n\n".join(sentences)
