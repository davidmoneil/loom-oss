"""Mode B: progressive compression with a summary+pointer pattern.

Replaces verbose segments (logs, file dumps, JSON arrays, repeated patterns,
error stacks) inside a pre-built prompt with a compact summary plus a pointer
back to the full data. Each compression is tracked via :class:`CompressionMetrics`
so quality and savings can be monitored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class SegmentType(Enum):
    LOG = "log"
    FILE_DUMP = "file_dump"
    REPEATED_PATTERN = "repeated_pattern"
    ERROR_STACK = "error_stack"
    VERBOSE_OUTPUT = "verbose_output"
    JSON_ARRAY = "json_array"
    PROSE = "prose"


@dataclass
class Segment:
    """A contiguous block of text classified for potential compression."""

    segment_type: SegmentType
    start_offset: int
    end_offset: int
    text: str
    line_count: int = 0
    estimated_tokens: int = 0
    compression_potential: float = 0.0  # 0.0 to 1.0
    source_hint: str = ""

    def __post_init__(self):
        self.line_count = self.text.count('\n')
        self.estimated_tokens = max(1, len(self.text) // 4)

    @property
    def is_compressible(self) -> bool:
        """Worth compressing: >50 tokens and >20% savings potential."""
        return self.estimated_tokens > 50 and self.compression_potential > 0.2


@dataclass
class CompressionMetrics:
    """Tracks the outcome of compressing one segment."""

    segment_id: str
    segment_type: SegmentType
    original_size: int
    compressed_size: int
    original_tokens: int
    compressed_tokens: int
    summary_length: int
    pointer_target: str
    model_used: str = "summaries"
    timestamp: datetime = field(default_factory=datetime.utcnow)
    quality_score: Optional[float] = None
    reviewed: bool = False

    @property
    def compression_ratio(self) -> float:
        return (self.original_tokens - self.compressed_tokens) / max(1, self.original_tokens)

    @property
    def savings_pct(self) -> float:
        return self.compression_ratio * 100.0


@dataclass
class CompressedSegment:
    """Result of compressing a segment."""

    original: Segment
    summary: str
    pointer: str
    metrics: CompressionMetrics


class SegmentClassifier:
    """Identifies compressible segments in text."""

    RE_LOG_LINE = re.compile(
        r'(?:\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}|'
        r'[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}|'
        r'^\[[\d:]+\]|'
        r'^\d{2}:\d{2}:\d{2})',
        re.MULTILINE,
    )
    RE_ERROR_STACK = re.compile(
        r'^\s+at\s+\w+|'
        r'^\s+File\s+"[^"]+"|'
        r'^Traceback|'
        r'^Error:|'
        r'^Exception:',
        re.MULTILINE,
    )
    RE_JSON_ARRAY_START = re.compile(r'^\s*\[\s*\{', re.MULTILINE)
    RE_REPEATED_PATTERN = re.compile(r'^(.{10,})\n\1', re.MULTILINE)

    MIN_SEGMENT_TOKENS = 50
    MIN_SEGMENT_LINES = 3

    def classify(self, text: str, source_hint: str = "") -> List[Segment]:
        """Identify and classify segments, sorted by start_offset."""
        segments: List[Segment] = []
        lines = text.split('\n')

        segments.extend(self._find_log_segments(text, lines))
        segments.extend(self._find_error_segments(text, lines))
        segments.extend(self._find_json_array_segments(text))
        segments.extend(self._find_repeated_segments(text))

        segments = [
            s for s in segments
            if s.estimated_tokens >= self.MIN_SEGMENT_TOKENS
            and s.line_count >= self.MIN_SEGMENT_LINES
        ]
        segments.sort(key=lambda s: s.start_offset)
        return segments

    def _find_log_segments(self, text: str, lines: List[str]) -> List[Segment]:
        segments = []
        segment_start = None
        segment_lines = 0

        for i, line in enumerate(lines):
            is_log_line = self.RE_LOG_LINE.search(line) or (
                segment_start is not None and len(line.strip()) > 0
            )

            if is_log_line:
                if segment_start is None:
                    segment_start = i
                segment_lines += 1
            else:
                if segment_start is not None and segment_lines >= self.MIN_SEGMENT_LINES:
                    start_offset = sum(len(l) + 1 for l in lines[:segment_start])
                    end_offset = sum(len(l) + 1 for l in lines[:segment_start + segment_lines])
                    segment_text = '\n'.join(lines[segment_start:segment_start + segment_lines])
                    seg = Segment(
                        segment_type=SegmentType.LOG,
                        start_offset=start_offset,
                        end_offset=end_offset,
                        text=segment_text,
                        line_count=segment_lines,
                    )
                    seg.compression_potential = 0.75
                    segments.append(seg)
                segment_start = None
                segment_lines = 0

        if segment_start is not None and segment_lines >= self.MIN_SEGMENT_LINES:
            start_offset = sum(len(l) + 1 for l in lines[:segment_start])
            end_offset = len(text)
            segment_text = '\n'.join(lines[segment_start:])
            seg = Segment(
                segment_type=SegmentType.LOG,
                start_offset=start_offset,
                end_offset=end_offset,
                text=segment_text,
                line_count=segment_lines,
            )
            seg.compression_potential = 0.75
            segments.append(seg)

        return segments

    def _find_error_segments(self, text: str, lines: List[str]) -> List[Segment]:
        segments = []
        segment_start = None

        for i, line in enumerate(lines):
            if self.RE_ERROR_STACK.search(line):
                if segment_start is None:
                    segment_start = i
            else:
                if segment_start is not None:
                    segment_lines = i - segment_start
                    if segment_lines >= self.MIN_SEGMENT_LINES:
                        start_offset = sum(len(l) + 1 for l in lines[:segment_start])
                        end_offset = sum(len(l) + 1 for l in lines[:i])
                        segment_text = '\n'.join(lines[segment_start:i])
                        seg = Segment(
                            segment_type=SegmentType.ERROR_STACK,
                            start_offset=start_offset,
                            end_offset=end_offset,
                            text=segment_text,
                            line_count=segment_lines,
                        )
                        seg.compression_potential = 0.60
                        segments.append(seg)
                    segment_start = None

        return segments

    def _find_json_array_segments(self, text: str) -> List[Segment]:
        segments = []
        for match in self.RE_JSON_ARRAY_START.finditer(text):
            start = match.start()
            bracket_count = 1
            pos = match.start(0) + 1
            while pos < len(text) and bracket_count > 0:
                if text[pos] == '{':
                    bracket_count += 1
                elif text[pos] == '}':
                    bracket_count -= 1
                pos += 1

            if bracket_count == 0:
                segment_text = text[start:pos]
                if len(segment_text) > 200:
                    lines = segment_text.count('\n')
                    if lines >= self.MIN_SEGMENT_LINES:
                        seg = Segment(
                            segment_type=SegmentType.JSON_ARRAY,
                            start_offset=start,
                            end_offset=pos,
                            text=segment_text,
                            line_count=lines,
                        )
                        seg.compression_potential = 0.70
                        segments.append(seg)

        return segments

    def _find_repeated_segments(self, text: str) -> List[Segment]:
        segments = []
        lines = text.split('\n')
        counter: Dict[str, List[int]] = {}

        for i, line in enumerate(lines):
            normalized = line.strip()
            if len(normalized) > 20:
                counter.setdefault(normalized, []).append(i)

        for pattern, indices in counter.items():
            if len(indices) >= 5:
                start_line = indices[0]
                end_line = indices[-1]
                start_offset = sum(len(l) + 1 for l in lines[:start_line])
                end_offset = sum(len(l) + 1 for l in lines[:end_line + 1])
                segment_text = '\n'.join(lines[start_line:end_line + 1])
                seg = Segment(
                    segment_type=SegmentType.REPEATED_PATTERN,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    text=segment_text,
                    line_count=end_line - start_line + 1,
                )
                seg.compression_potential = 0.80
                segments.append(seg)

        return segments


class SummaryGenerator:
    """Generates summaries with pointers to full data."""

    @staticmethod
    def generate(segment: Segment) -> Tuple[str, str]:
        if segment.segment_type == SegmentType.LOG:
            return SummaryGenerator._summarize_log(segment)
        if segment.segment_type == SegmentType.ERROR_STACK:
            return SummaryGenerator._summarize_error(segment)
        if segment.segment_type == SegmentType.JSON_ARRAY:
            return SummaryGenerator._summarize_json_array(segment)
        if segment.segment_type == SegmentType.REPEATED_PATTERN:
            return SummaryGenerator._summarize_repeated(segment)
        return (f"[{len(segment.text)} chars compressed]", segment.source_hint)

    @staticmethod
    def _summarize_log(segment: Segment) -> Tuple[str, str]:
        lines = segment.text.split('\n')
        error_count = sum(1 for l in lines if 'error' in l.lower())
        warn_count = sum(1 for l in lines if 'warn' in l.lower())
        info_count = sum(1 for l in lines if 'info' in l.lower())

        summary_parts = []
        if error_count > 0:
            summary_parts.append(f"{error_count} errors")
        if warn_count > 0:
            summary_parts.append(f"{warn_count} warnings")
        if info_count > 0:
            summary_parts.append(f"{info_count} info messages")
        if not summary_parts:
            summary_parts.append(f"{len(lines)} log lines")

        source = segment.source_hint or "log output"
        summary = f"[{', '.join(summary_parts)} in {source}]"
        pointer = f"Full log at {segment.source_hint}:{segment.start_offset}-{segment.end_offset}"
        return (summary, pointer)

    @staticmethod
    def _summarize_error(segment: Segment) -> Tuple[str, str]:
        lines = segment.text.split('\n')
        error_type = "Error"
        for line in lines:
            if 'Exception' in line or 'Error' in line:
                error_type = line.strip()[:60]
                break
        summary = f"[{error_type}... ({segment.line_count} lines)]"
        pointer = f"Full stack at lines {segment.start_offset}-{segment.end_offset}"
        return (summary, pointer)

    @staticmethod
    def _summarize_json_array(segment: Segment) -> Tuple[str, str]:
        try:
            data = json.loads(segment.text)
            if isinstance(data, list):
                count = len(data)
                if count > 0 and isinstance(data[0], dict):
                    keys = list(data[0].keys())[:3]
                    summary = f"[JSON array: {count} items with keys {keys}]"
                else:
                    summary = f"[JSON array: {count} items]"
            else:
                summary = f"[JSON object, {len(str(data))} chars]"
        except json.JSONDecodeError:
            summary = f"[JSON data, {len(segment.text)} chars]"
        pointer = f"Full JSON at {segment.source_hint}:{segment.start_offset}-{segment.end_offset}"
        return (summary, pointer)

    @staticmethod
    def _summarize_repeated(segment: Segment) -> Tuple[str, str]:
        lines = segment.text.split('\n')
        unique_lines = len(set(lines))
        total_lines = len(lines)
        summary = (
            f"[{total_lines} lines, {unique_lines} unique patterns "
            f"({100 * unique_lines // max(1, total_lines)}% unique)]"
        )
        pointer = f"Full data at lines {segment.start_offset}-{segment.end_offset}"
        return (summary, pointer)


class ModeBProcessor:
    """Orchestrates Mode B progressive compression with quality tracking."""

    def __init__(self, enable_compression: bool = True):
        self.enabled = enable_compression
        self.classifier = SegmentClassifier()
        self.metrics: List[CompressionMetrics] = []

    def compress(self, text: str, source_hint: str = "") -> Tuple[str, List[CompressionMetrics]]:
        """Compress text by replacing compressible segments with summaries."""
        if not self.enabled:
            return (text, [])

        segments = self.classifier.classify(text, source_hint)
        compressible = [s for s in segments if s.is_compressible]
        if not compressible:
            return (text, [])

        compressed_parts = []
        last_offset = 0
        metrics_list = []

        for segment in compressible:
            if segment.start_offset > last_offset:
                compressed_parts.append(text[last_offset:segment.start_offset])

            summary, pointer = SummaryGenerator.generate(segment)
            compressed_text = f"{summary}\n[→ {pointer}]"
            compressed_parts.append(compressed_text)

            metric = CompressionMetrics(
                segment_id=f"{source_hint}:{segment.start_offset}",
                segment_type=segment.segment_type,
                original_size=len(segment.text),
                compressed_size=len(compressed_text),
                original_tokens=segment.estimated_tokens,
                compressed_tokens=len(compressed_text) // 4,
                summary_length=len(summary),
                pointer_target=pointer,
            )
            metrics_list.append(metric)
            self.metrics.append(metric)
            last_offset = segment.end_offset

        if last_offset < len(text):
            compressed_parts.append(text[last_offset:])

        return (''.join(compressed_parts), metrics_list)

    def get_compression_stats(self) -> Dict[str, Any]:
        """Aggregate compression statistics across all processed segments."""
        if not self.metrics:
            return {
                "total_segments": 0,
                "total_original_tokens": 0,
                "total_compressed_tokens": 0,
                "average_compression_ratio": 0.0,
                "by_type": {},
            }

        total_original = sum(m.original_tokens for m in self.metrics)
        total_compressed = sum(m.compressed_tokens for m in self.metrics)

        by_type: Dict[SegmentType, Dict[str, int]] = {}
        for m in self.metrics:
            bucket = by_type.setdefault(
                m.segment_type, {"count": 0, "original": 0, "compressed": 0}
            )
            bucket["count"] += 1
            bucket["original"] += m.original_tokens
            bucket["compressed"] += m.compressed_tokens

        return {
            "total_segments": len(self.metrics),
            "total_original_tokens": total_original,
            "total_compressed_tokens": total_compressed,
            "average_compression_ratio": (
                (total_original - total_compressed) / max(1, total_original)
                if total_original > 0 else 0.0
            ),
            "by_type": {name.value: stats for name, stats in by_type.items()},
            "timestamp": datetime.utcnow().isoformat(),
        }

    def rollback_to_original(self) -> None:
        """Disable compression — pass through unchanged."""
        self.enabled = False
