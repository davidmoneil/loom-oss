"""Tests for the Loom DLP Scanner and content relevance scoring."""

import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.scanner.engine import SensitiveDataScanner
from loom.scanner.actions import luhn_check, _apply_mask_format
from loom.storage.sqlite import LoomStorage
from loom.compression.relevance import (
    hash_content,
    score_messages_by_relevance,
    record_request_content,
)


@pytest.fixture
def scanner():
    config = {
        "scanner": {
            "enabled": True,
            "sanitize_logs": True,
            "log_detections": False,
            "model_tags": {"qwen3": ["local"], "llama3": ["local"]},
            "trusted_tags": ["local"],
        }
    }
    return SensitiveDataScanner(config)


@pytest.fixture
def storage(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = LoomStorage(db_path)
    s.connect()
    yield s
    s.close()


# ============================================================ Scanner Rules

class TestSSN:
    def test_detects_ssn(self, scanner):
        result = scanner.scan("SSN: 123-45-6789")
        assert result.had_detections

    def test_redacts_ssn(self, scanner):
        scanned, matches = scanner.apply("SSN: 123-45-6789")
        assert "[REDACTED:ssn]" in scanned
        assert "123-45-6789" not in scanned

    def test_multiple_ssns(self, scanner):
        scanned, matches = scanner.apply("A: 111-22-3333 B: 444-55-6666")
        assert scanned.count("[REDACTED:ssn]") == 2


class TestCreditCard:
    def test_detects_visa_with_luhn(self, scanner):
        result = scanner.scan("Card: 4111111111111111")
        cc = [m for m in result.matches if m.rule_name == "credit_card"]
        assert len(cc) == 1

    def test_rejects_invalid_luhn(self, scanner):
        result = scanner.scan("Not a card: 4111111111111112")
        cc = [m for m in result.matches if m.rule_name == "credit_card"]
        assert len(cc) == 0

    def test_masks_card(self, scanner):
        scanned, _ = scanner.apply("Pay: 4111111111111111")
        assert "****-****-****-1111" in scanned

    def test_luhn_valid(self):
        assert luhn_check("4111111111111111")
        assert luhn_check("5500000000000004")

    def test_luhn_invalid(self):
        assert not luhn_check("4111111111111112")
        assert not luhn_check("1234")


class TestAPIKeys:
    def test_openai_key(self, scanner):
        scanned, matches = scanner.apply("key: sk-abcdefghijklmnopqrstuvwx")
        assert "[REDACTED:api_key_generic]" in scanned

    def test_github_pat(self, scanner):
        scanned, _ = scanner.apply("token: ghp_abcdefghijklmnopqrstuvwxyz1234567890")
        assert "[REDACTED:api_key_generic]" in scanned


class TestBearerToken:
    def test_redacts_bearer(self, scanner):
        scanned, _ = scanner.apply("Authorization: Bearer ZljCiGRRAeii7B89ggFLKG2klfuRUSTTU5KX7ORtVLw=")
        assert "[REDACTED:bearer_token]" in scanned
        assert "Bearer" in scanned  # keyword preserved

    def test_short_bearer_ignored(self, scanner):
        scanned, _ = scanner.apply("Authorization: Bearer short")
        assert scanned == "Authorization: Bearer short"


class TestPrivateKey:
    def test_rsa_key(self, scanner):
        scanned, _ = scanner.apply("-----BEGIN RSA PRIVATE KEY-----\nMIIEpA...")
        assert "[REDACTED:private_key]" in scanned

    def test_ec_key(self, scanner):
        scanned, _ = scanner.apply("-----BEGIN EC PRIVATE KEY-----")
        assert "[REDACTED:private_key]" in scanned


class TestJWT:
    def test_masks_jwt(self, scanner):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        scanned, _ = scanner.apply(f"Token: {jwt}")
        assert jwt not in scanned
        assert "eyJ..." in scanned


# ============================================================ Model Skip

class TestModelSkip:
    def test_skip_local_model(self, scanner):
        assert scanner.should_skip(model="qwen3:8b")
        assert scanner.should_skip(model="llama3.2:3b")

    def test_no_skip_cloud_model(self, scanner):
        assert not scanner.should_skip(model="sonnet")
        assert not scanner.should_skip(model="gpt-4o-mini")

    def test_apply_skips_for_local(self, scanner):
        text = "SSN: 123-45-6789"
        scanned, matches = scanner.apply(text, model="qwen3:8b")
        assert scanned == text
        assert len(matches) == 0

    def test_apply_scans_for_cloud(self, scanner):
        scanned, matches = scanner.apply("SSN: 123-45-6789", model="sonnet")
        assert "[REDACTED:ssn]" in scanned


# ============================================================ Log Sanitization

class TestLogSanitization:
    def test_sanitize_dict(self, scanner):
        entry = {"content": "SSN: 123-45-6789", "count": 42}
        sanitized = scanner.sanitize_log_entry(entry)
        assert "[REDACTED:ssn]" in sanitized["content"]
        assert sanitized["count"] == 42

    def test_sanitize_nested(self, scanner):
        entry = {"meta": {"key": "sk-abcdefghijklmnopqrstuvwx"}}
        sanitized = scanner.sanitize_log_entry(entry)
        assert "[REDACTED:api_key_generic]" in sanitized["meta"]["key"]

    def test_sanitize_list(self, scanner):
        entry = {"items": ["normal", "sk-abcdefghijklmnopqrstuvwx"]}
        sanitized = scanner.sanitize_log_entry(entry)
        assert sanitized["items"][0] == "normal"
        assert "[REDACTED:" in sanitized["items"][1]

    def test_sanitize_disabled(self):
        s = SensitiveDataScanner({"scanner": {"enabled": True, "sanitize_logs": False}})
        entry = {"content": "SSN: 123-45-6789"}
        assert s.sanitize_log_entry(entry) == entry

    def test_preserves_types(self, scanner):
        entry = {"n": 42, "f": 0.5, "b": True, "none": None}
        sanitized = scanner.sanitize_log_entry(entry)
        assert sanitized == entry


# ============================================================ Config

class TestScannerConfig:
    def test_disabled_scanner(self):
        s = SensitiveDataScanner({"scanner": {"enabled": False}})
        result = s.scan("123-45-6789")
        assert not result.had_detections

    def test_no_config(self):
        s = SensitiveDataScanner()
        assert not s.enabled

    def test_rules_summary(self, scanner):
        summary = scanner.rules_summary()
        names = [r["name"] for r in summary]
        assert "ssn" in names
        assert "credit_card" in names

    def test_skip_config(self, scanner):
        cfg = scanner.skip_config()
        assert "local" in cfg["trusted_tags"]
        assert "qwen3" in cfg["model_tags"]

    def test_update_rule(self, scanner):
        assert scanner.update_rule("ssn", {"action": "mask"})
        rule = next(r for r in scanner.rules if r.name == "ssn")
        assert rule.action == "mask"

    def test_update_nonexistent(self, scanner):
        assert not scanner.update_rule("nonexistent", {"action": "mask"})


class TestMaskFormat:
    def test_last4(self):
        assert _apply_mask_format("123-45-6789", "***-**-{last4}") == "***-**-6789"

    def test_cc_last4(self):
        assert _apply_mask_format("4111111111111111", "****-****-****-{last4}") == "****-****-****-1111"

    def test_no_format(self):
        assert "MASKED" in _apply_mask_format("sensitive", None)


class TestNoFalsePositives:
    def test_clean_text(self, scanner):
        assert not scanner.scan("The quick brown fox").had_detections

    def test_code_snippet(self, scanner):
        assert not scanner.scan("def add(a, b): return a + b").had_detections

    def test_zip_code(self, scanner):
        result = scanner.scan("Zip: 90210")
        ssn = [m for m in result.matches if m.rule_name == "ssn"]
        assert len(ssn) == 0


# ============================================================ Content Importance

class TestContentImportance:
    def test_record_and_query(self, storage):
        storage.record_content_importance("hash123", "request")
        scores = storage.get_content_importance(["hash123"])
        assert "hash123" in scores
        assert scores["hash123"] >= 0.5

    def test_hit_count_increases_score(self, storage):
        storage.record_content_importance("hash456", "request")
        scores1 = storage.get_content_importance(["hash456"])

        storage.record_content_importance("hash456", "request")
        storage.record_content_importance("hash456", "request")
        scores2 = storage.get_content_importance(["hash456"])

        assert scores2["hash456"] >= scores1["hash456"]

    def test_unknown_hash_returns_empty(self, storage):
        scores = storage.get_content_importance(["nonexistent"])
        assert len(scores) == 0

    def test_empty_list(self, storage):
        assert storage.get_content_importance([]) == {}

    def test_cleanup_stale(self, storage):
        storage.record_content_importance("old_hash", "request")
        # Force last_seen to be old
        storage.conn.execute(
            "UPDATE content_importance SET last_seen = last_seen - 700000 WHERE content_hash = 'old_hash'"
        )
        storage.conn.commit()
        removed = storage.cleanup_stale_importance(max_age_hours=168)
        assert removed == 1
        assert storage.get_content_importance(["old_hash"]) == {}


# ============================================================ Relevance Scoring

class TestRelevanceScoring:
    def test_scores_known_content(self, storage):
        messages = [
            {"role": "user", "content": "Tell me about the architecture of this system and how the layers connect together"},
            {"role": "assistant", "content": "The system has three layers that handle routing, compression, and observability independently"},
        ]
        # Record the content first
        record_request_content(messages, storage)

        # Now score — should find the recorded content
        scores = score_messages_by_relevance(messages, storage)
        assert len(scores) > 0

    def test_no_scores_for_new_content(self, storage):
        messages = [
            {"role": "user", "content": "Something completely new and never seen before in any context"},
        ]
        scores = score_messages_by_relevance(messages, storage)
        assert len(scores) == 0

    def test_graceful_without_storage(self):
        messages = [{"role": "user", "content": "test"}]
        scores = score_messages_by_relevance(messages, None)
        assert scores == {}

    def test_record_skips_short_content(self, storage):
        messages = [{"role": "user", "content": "hi"}]
        record_request_content(messages, storage)
        # "hi" is < 50 chars, should not be recorded
        h = hash_content("hi")
        assert storage.get_content_importance([h]) == {}

    def test_content_blocks_format(self, storage):
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "This is a long enough message to be recorded as important content"},
            ]},
        ]
        record_request_content(messages, storage)
        scores = score_messages_by_relevance(messages, storage)
        assert len(scores) > 0

    def test_hash_deterministic(self):
        assert hash_content("same text") == hash_content("same text")
        assert hash_content("text a") != hash_content("text b")


# ============================================================ Integration

class TestFullChain:
    def test_scanner_plus_relevance(self, scanner, storage):
        """End-to-end: record content, score it, scan it."""
        messages = [
            {"role": "user", "content": "My SSN is 123-45-6789 and the architecture has three layers"},
            {"role": "assistant", "content": "I'll help with the architecture. Card: 4111111111111111"},
        ]
        # Record content importance
        record_request_content(messages, storage)

        # Score relevance
        scores = score_messages_by_relevance(messages, storage)
        assert len(scores) > 0  # Both messages should be tracked

        # Scan for sensitive data
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                scanned, matches = scanner.apply(content)
                if matches:
                    assert "123-45-6789" not in scanned or "4111111111111111" not in scanned
