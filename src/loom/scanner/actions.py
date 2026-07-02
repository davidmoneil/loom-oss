"""Action implementations for the sensitive data scanner.

Each action transforms a matched sensitive value. Actions are pure functions
except for pseudonymize, which needs session context for consistency.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ActionContext:
    session_id: str = "unknown"
    source: str = "unknown"
    provider: str = "unknown"


def _extract_last_n(text: str, n: int) -> str:
    digits = re.sub(r"[^0-9]", "", text)
    return digits[-n:] if len(digits) >= n else digits


def _apply_mask_format(matched_text: str, mask_format: Optional[str]) -> str:
    if not mask_format:
        return f"[MASKED:{len(matched_text)} chars]"
    result = mask_format
    last_n_match = re.search(r"\{last(\d+)\}", mask_format)
    if last_n_match:
        n = int(last_n_match.group(1))
        last_chars = _extract_last_n(matched_text, n)
        result = result.replace(last_n_match.group(0), last_chars)
    return result


def luhn_check(number_str: str) -> bool:
    digits = re.sub(r"[^0-9]", "", number_str)
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# Pseudonymization: deterministic fake data from seeded hash
_FAKE_GENERATORS = {
    "ssn": lambda seed: f"{(seed % 899) + 100:03d}-{(seed % 99) + 1:02d}-{(seed % 9999) + 1:04d}",
    "credit_card": lambda seed: f"****-****-****-{(seed % 9999) + 1:04d}",
    "email": lambda seed: f"user{seed % 10000}@example.com",
    "phone_us": lambda seed: f"(555) 000-{(seed % 9999) + 1:04d}",
    "ip_address": lambda seed: f"198.51.100.{(seed % 254) + 1}",
}

_pseudonym_maps: dict[str, dict[str, str]] = {}


def _pseudonymize(matched_text: str, rule_name: str, session_id: str) -> str:
    key = f"{session_id}:{rule_name}"
    if key not in _pseudonym_maps:
        _pseudonym_maps[key] = {}
    session_map = _pseudonym_maps[key]

    text_hash = hashlib.sha256(matched_text.encode()).hexdigest()[:16]
    if text_hash in session_map:
        return session_map[text_hash]

    seed_input = f"{session_id}:{matched_text}"
    seed = int(hashlib.sha256(seed_input.encode()).hexdigest()[:8], 16)
    generator = _FAKE_GENERATORS.get(rule_name)
    fake = generator(seed) if generator else f"[PSEUDO:{rule_name}:{seed % 10000}]"
    session_map[text_hash] = fake
    return fake


def apply_action(
    matched_text: str,
    rule_name: str,
    action: str,
    mask_format: Optional[str],
    ctx: ActionContext,
) -> str:
    if action == "redact":
        return f"[REDACTED:{rule_name}]"
    if action == "mask":
        return _apply_mask_format(matched_text, mask_format)
    if action == "pseudonymize":
        try:
            from . import pseudonymizer

            return pseudonymizer.pseudonymize(matched_text, rule_name, ctx.session_id)
        except ImportError:
            return _pseudonymize(matched_text, rule_name, ctx.session_id)
    if action == "encrypt":
        try:
            from . import crypto

            return crypto.encrypt(matched_text, ctx.session_id)
        except (ImportError, Exception):
            return f"[ENCRYPTED:unavailable]"
    if action == "log_only":
        return matched_text
    return matched_text
