"""Throttle governor settings management.

Ported from the Nexus dashboard's GovernorCard for feature parity: the same
data model (enabled flag, per-tier utilization thresholds, per-job class
overrides) and the same validation/audit rules, persisted to a JSON file so
edits made from the Loom-OSS dashboard survive a restart.

Loom-OSS is a request proxy, not a job scheduler, so there is no dispatcher
here to actually throttle against these settings yet — this module is the
settings + audit surface only. `status()` reports the configured tier
thresholds and override count; live utilization/throttle counters are left
at zero until a caller wires in real usage tracking.

Follows loom-oss conventions: defensive imports, graceful degradation,
never raises into the request path.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

VALID_THROTTLE_CLASSES = ("critical", "high", "standard", "low")

_DEFAULT_TIER_THRESHOLDS = {
    "moderate": 50,
    "elevated": 70,
    "high": 85,
    "critical": 95,
}

_DATA_DIR = Path(os.environ.get("LOOM_GOVERNOR_DATA_DIR", "logs"))
_SETTINGS_PATH = _DATA_DIR / "governor_settings.json"
_AUDIT_PATH = _DATA_DIR / "governor_audit.jsonl"


class GovernorValidationError(ValueError):
    """Raised when an update to the governor settings fails validation."""


@dataclass
class ThrottleGovernorSettings:
    enabled: bool = True
    tier_thresholds: dict[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_TIER_THRESHOLDS)
    )
    class_overrides: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ThrottleGovernor:
    """Loads, validates, and persists throttle governor settings."""

    def __init__(
        self,
        settings_path: Optional[Path] = None,
        audit_path: Optional[Path] = None,
    ) -> None:
        self._settings_path = Path(settings_path) if settings_path else _SETTINGS_PATH
        self._audit_path = Path(audit_path) if audit_path else _AUDIT_PATH
        self._lock = threading.Lock()
        self._settings = self._load()

    # --------------------------------------------------------------- loading
    def _load(self) -> ThrottleGovernorSettings:
        try:
            raw = json.loads(self._settings_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return ThrottleGovernorSettings()

        thresholds = dict(_DEFAULT_TIER_THRESHOLDS)
        thresholds.update(raw.get("tier_thresholds") or {})
        return ThrottleGovernorSettings(
            enabled=bool(raw.get("enabled", True)),
            tier_thresholds=thresholds,
            class_overrides=dict(raw.get("class_overrides") or {}),
        )

    def _save(self) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._settings_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._settings.to_dict(), indent=2))
        tmp_path.replace(self._settings_path)

    def _append_audit(self, action: str, actor: str, details: dict[str, Any]) -> None:
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": time.time(),
                "action": action,
                "actor": actor,
                "details": details,
            }
            with self._audit_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # audit logging is best-effort, never blocks a settings change

    # ------------------------------------------------------------ validation
    @staticmethod
    def _validate_thresholds(thresholds: dict[str, int]) -> None:
        allowed_keys = set(_DEFAULT_TIER_THRESHOLDS)
        unknown = set(thresholds) - allowed_keys
        if unknown:
            raise GovernorValidationError(f"Unknown tier(s): {', '.join(sorted(unknown))}")
        for key, value in thresholds.items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise GovernorValidationError(f"Tier threshold '{key}' must be an integer")
            if not (0 <= value <= 100):
                raise GovernorValidationError(f"Tier threshold '{key}' must be between 0 and 100")
        ordered = [thresholds[k] for k in ("moderate", "elevated", "high", "critical")]
        if ordered != sorted(ordered):
            raise GovernorValidationError(
                "Tier thresholds must be non-decreasing: moderate <= elevated <= high <= critical"
            )

    @staticmethod
    def _validate_class_overrides(overrides: dict[str, str]) -> None:
        for job, cls in overrides.items():
            if cls not in VALID_THROTTLE_CLASSES:
                raise GovernorValidationError(
                    f"Invalid throttle class for {job}: {cls}. "
                    f"Valid: {', '.join(VALID_THROTTLE_CLASSES)}"
                )

    # ------------------------------------------------------------------- api
    def status(self) -> dict[str, Any]:
        s = self._settings
        tier = "normal"
        if s.enabled:
            for name in ("critical", "high", "elevated", "moderate"):
                if s.tier_thresholds.get(name, 100) <= 0:
                    tier = name
                    break
        return {
            "enabled": s.enabled,
            "tier": tier if s.enabled else "disabled",
            "util_5h": 0.0,
            "util_7d": 0.0,
            "jobs_throttled": 0,
            "jobs_skipped": 0,
            "multipliers": {},
            "override_count": len(s.class_overrides),
        }

    def get_settings(self) -> dict[str, Any]:
        return self._settings.to_dict()

    def update(self, updates: dict[str, Any], actor: str = "dashboard") -> dict[str, Any]:
        with self._lock:
            if "enabled" in updates:
                if not isinstance(updates["enabled"], bool):
                    raise GovernorValidationError("'enabled' must be a boolean")

            if "tier_thresholds" in updates:
                merged = dict(self._settings.tier_thresholds)
                merged.update(updates["tier_thresholds"] or {})
                self._validate_thresholds(merged)

            if "class_overrides" in updates:
                merged_overrides = dict(self._settings.class_overrides)
                merged_overrides.update(updates["class_overrides"] or {})
                self._validate_class_overrides(merged_overrides)

            # Validation passed for every field present — apply them all.
            if "enabled" in updates:
                self._settings.enabled = updates["enabled"]
            if "tier_thresholds" in updates:
                self._settings.tier_thresholds.update(updates["tier_thresholds"] or {})
            if "class_overrides" in updates:
                self._settings.class_overrides.update(updates["class_overrides"] or {})

            self._save()
            self._append_audit("update", actor, updates)
            return self._settings.to_dict()

    def delete_class_override(self, job: str, actor: str = "dashboard") -> dict[str, Any]:
        with self._lock:
            existed = self._settings.class_overrides.pop(job, None) is not None
            if existed:
                self._save()
                self._append_audit("delete_class_override", actor, {"job": job})
            return self._settings.to_dict()
