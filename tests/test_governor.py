"""Tests for the throttle governor settings module."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loom.governor import GovernorValidationError, ThrottleGovernor


@pytest.fixture
def governor(tmp_path):
    return ThrottleGovernor(
        settings_path=tmp_path / "governor_settings.json",
        audit_path=tmp_path / "governor_audit.jsonl",
    )


class TestDefaults:
    def test_defaults_enabled(self, governor):
        settings = governor.get_settings()
        assert settings["enabled"] is True
        assert settings["tier_thresholds"] == {
            "moderate": 50,
            "elevated": 70,
            "high": 85,
            "critical": 95,
        }
        assert settings["class_overrides"] == {}

    def test_status_reports_tier(self, governor):
        status = governor.status()
        assert status["tier"] == "normal"
        assert status["enabled"] is True


class TestUpdateEnabled:
    def test_toggle_enabled(self, governor):
        updated = governor.update({"enabled": False})
        assert updated["enabled"] is False
        assert governor.status()["tier"] == "disabled"

    def test_enabled_must_be_bool(self, governor):
        with pytest.raises(GovernorValidationError):
            governor.update({"enabled": "yes"})


class TestTierThresholds:
    def test_update_single_tier(self, governor):
        updated = governor.update({"tier_thresholds": {"moderate": 40}})
        assert updated["tier_thresholds"]["moderate"] == 40

    def test_rejects_out_of_range(self, governor):
        with pytest.raises(GovernorValidationError):
            governor.update({"tier_thresholds": {"moderate": 150}})

    def test_rejects_non_monotonic(self, governor):
        with pytest.raises(GovernorValidationError):
            governor.update({"tier_thresholds": {"moderate": 90, "elevated": 70}})

    def test_rejects_unknown_tier(self, governor):
        with pytest.raises(GovernorValidationError):
            governor.update({"tier_thresholds": {"turbo": 10}})


class TestClassOverrides:
    def test_add_override(self, governor):
        updated = governor.update({"class_overrides": {"nightly-digest": "low"}})
        assert updated["class_overrides"]["nightly-digest"] == "low"

    def test_rejects_invalid_class(self, governor):
        with pytest.raises(GovernorValidationError):
            governor.update({"class_overrides": {"job": "urgent"}})

    def test_delete_override(self, governor):
        governor.update({"class_overrides": {"job-a": "high"}})
        updated = governor.delete_class_override("job-a")
        assert "job-a" not in updated["class_overrides"]


class TestPersistence:
    def test_survives_reload(self, tmp_path):
        settings_path = tmp_path / "governor_settings.json"
        audit_path = tmp_path / "governor_audit.jsonl"
        gov1 = ThrottleGovernor(settings_path=settings_path, audit_path=audit_path)
        gov1.update({"enabled": False, "class_overrides": {"job-a": "critical"}})

        gov2 = ThrottleGovernor(settings_path=settings_path, audit_path=audit_path)
        settings = gov2.get_settings()
        assert settings["enabled"] is False
        assert settings["class_overrides"]["job-a"] == "critical"

    def test_writes_audit_entries(self, governor, tmp_path):
        governor.update({"enabled": False})
        audit_path = tmp_path / "governor_audit.jsonl"
        assert audit_path.exists()
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 1
