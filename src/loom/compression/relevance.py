"""Content relevance scoring for compression decisions.

Scores messages by checking their content hashes against the
content_importance table in SQLite. Messages that have been seen
in prior successful requests get a high relevance score, which
tells the compression engine to preserve them (reduce age_ratio).

This is the SQLite equivalent of the internal Loom's Postgres
loom_embeddings lookup — same binary signal (seen = important),
lighter storage backend.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger("loom.compression.relevance")


def hash_content(text: str) -> str:
    return hashlib.sha256(text[:512].encode()).hexdigest()[:16]


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def score_messages_by_relevance(
    messages: list[dict],
    storage: Any,
) -> dict[int, float]:
    """Return per-message relevance scores (0.0-1.0) from SQLite content_importance.

    High scores = high-signal content, compress less aggressively.
    Returns empty dict if storage is unavailable (pure age-ratio fallback).
    """
    if storage is None:
        return {}

    try:
        hashes_by_idx: dict[int, str] = {}
        for idx, msg in enumerate(messages):
            text = _extract_text(msg.get("content", ""))
            if text:
                hashes_by_idx[idx] = hash_content(text)

        if not hashes_by_idx:
            return {}

        importance = storage.get_content_importance(list(hashes_by_idx.values()))
        scores: dict[int, float] = {}
        for idx, h in hashes_by_idx.items():
            if h in importance:
                scores[idx] = importance[h]
        return scores

    except Exception:
        logger.debug("Relevance scoring failed, falling back to age-ratio", exc_info=True)
        return {}


def record_request_content(
    messages: list[dict],
    storage: Any,
    source: str = "request",
) -> None:
    """Record content hashes from a successful request for future relevance scoring.

    Called after a request completes successfully. The content_importance table
    tracks which content has appeared in real conversations, building up the
    signal that the compression engine uses to preserve important messages.
    """
    if storage is None:
        return

    try:
        for msg in messages:
            text = _extract_text(msg.get("content", ""))
            if text and len(text) > 50:
                h = hash_content(text)
                storage.record_content_importance(h, source)
    except Exception:
        logger.debug("Failed to record content importance", exc_info=True)
