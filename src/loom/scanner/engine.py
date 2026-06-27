"""Core scanning engine for the Loom Sensitive Data Scanner.

Loads rules from config (Pydantic model or external YAML file), compiles
regex patterns, and applies configured actions to detected sensitive data.

Follows loom-oss conventions: defensive imports, graceful degradation,
never raises into the request path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from .actions import ActionContext, apply_action, luhn_check
from .rules import DEFAULT_RULES

logger = logging.getLogger("loom.scanner")

_CACHE_TTL_SECONDS = 60
_DETECTIONS_DIR = Path(os.environ.get("LOOM_SCANNER_LOG_DIR", "logs"))


@dataclass
class ScanRule:
    name: str
    description: str
    patterns: list[re.Pattern]
    action: str
    enabled: bool
    mask_format: Optional[str] = None
    streaming_mode: str = "buffer"


@dataclass
class ScanMatch:
    rule_name: str
    start: int
    end: int
    replacement: str


@dataclass
class ScanResult:
    text: str
    matches: list[ScanMatch]
    had_detections: bool


class SensitiveDataScanner:
    """Thread-safe DLP scanner with hot-reloading config."""

    def __init__(self, config: Any = None) -> None:
        self._lock = threading.Lock()
        self._rules: list[ScanRule] = []
        self._enabled = False
        self._sanitize_logs = True
        self._content_logging = "off"
        self._streaming_mode_default = "buffer"
        self._log_detections = True
        self._skip_providers: set[str] = set()
        self._skip_models: set[str] = set()
        self._model_tags: dict[str, list[str]] = {}
        self._trusted_tags: set[str] = set()
        self._rules_path: Optional[Path] = None
        self._rules_mtime: float = 0.0
        self._loaded_at: float = 0.0

        # Stats (in-memory, reset on restart)
        self._total_scans = 0
        self._total_detections = 0
        self._detections_by_rule: dict[str, int] = {}

        self._load_from_config(config)

    def _load_from_config(self, config: Any) -> None:
        scanner_cfg: dict = {}

        if config is not None and hasattr(config, "scanner"):
            sc = config.scanner
            if isinstance(sc, dict):
                scanner_cfg = sc
            elif hasattr(sc, "model_dump"):
                scanner_cfg = sc.model_dump()
            elif hasattr(sc, "dict"):
                scanner_cfg = sc.dict()
            elif hasattr(sc, "__dict__"):
                scanner_cfg = {k: v for k, v in vars(sc).items() if not k.startswith("_")}
        elif isinstance(config, dict):
            scanner_cfg = config.get("scanner", {})

        self._enabled = scanner_cfg.get("enabled", False)
        self._sanitize_logs = scanner_cfg.get("sanitize_logs", True)
        self._content_logging = scanner_cfg.get("content_logging", "off")
        self._streaming_mode_default = scanner_cfg.get("streaming_mode", "buffer")
        self._log_detections = scanner_cfg.get("log_detections", True)
        self._skip_providers = set(scanner_cfg.get("skip_providers", []))
        self._skip_models = set(scanner_cfg.get("skip_models", []))
        self._model_tags = scanner_cfg.get("model_tags", {})
        self._trusted_tags = set(scanner_cfg.get("trusted_tags", []))

        rules_path = scanner_cfg.get("rules_path", "")
        if rules_path and Path(rules_path).is_file():
            self._rules_path = Path(rules_path)
            self._load_rules_from_file()
        else:
            rules_data = scanner_cfg.get("rules", DEFAULT_RULES)
            self._compile_rules(rules_data)

        self._loaded_at = time.monotonic()

    def _load_rules_from_file(self) -> None:
        if self._rules_path is None:
            return
        try:
            raw = yaml.safe_load(self._rules_path.read_text())
            rules_data = raw.get("rules", []) if isinstance(raw, dict) else raw
            self._compile_rules(rules_data)
            self._rules_mtime = self._rules_path.stat().st_mtime
        except Exception as e:
            logger.warning("Failed to load scanner rules from %s: %s", self._rules_path, e)

    def _compile_rules(self, rules_data: list[dict]) -> None:
        rules = []
        for rd in rules_data:
            if not isinstance(rd, dict):
                continue
            try:
                compiled = [re.compile(p) for p in rd.get("patterns", [])]
                rules.append(ScanRule(
                    name=rd["name"],
                    description=rd.get("description", ""),
                    patterns=compiled,
                    action=rd.get("action", "log_only"),
                    enabled=rd.get("enabled", True),
                    mask_format=rd.get("mask_format"),
                    streaming_mode=rd.get("streaming_mode", self._streaming_mode_default),
                ))
            except Exception as e:
                logger.warning("Skipping invalid scanner rule %s: %s", rd.get("name", "?"), e)
        self._rules = rules
        logger.info("Scanner loaded %d rules (%d enabled)", len(rules), sum(1 for r in rules if r.enabled))

    def _maybe_reload(self) -> None:
        if self._rules_path is None:
            return
        now = time.monotonic()
        if now - self._loaded_at < _CACHE_TTL_SECONDS:
            return
        try:
            mtime = self._rules_path.stat().st_mtime
            if mtime != self._rules_mtime:
                with self._lock:
                    if mtime != self._rules_mtime:
                        self._load_rules_from_file()
                        self._loaded_at = time.monotonic()
            else:
                self._loaded_at = now
        except FileNotFoundError:
            pass

    @property
    def enabled(self) -> bool:
        self._maybe_reload()
        return self._enabled

    @property
    def rules(self) -> list[ScanRule]:
        self._maybe_reload()
        return self._rules

    @property
    def content_logging(self) -> str:
        return self._content_logging

    def should_skip(self, provider: str = "", model: str = "") -> bool:
        if provider and provider in self._skip_providers:
            return True
        if model and model in self._skip_models:
            return True
        if model:
            for prefix, tags in self._model_tags.items():
                if model.startswith(prefix):
                    if self._trusted_tags & set(tags):
                        return True
        return False

    def scan(self, text: str, provider: str = "", model: str = "") -> ScanResult:
        self._maybe_reload()
        self._total_scans += 1
        if not self._enabled or not text or self.should_skip(provider, model):
            return ScanResult(text=text, matches=[], had_detections=False)

        matches = []
        for rule in self._rules:
            if not rule.enabled:
                continue
            for pattern in rule.patterns:
                for m in pattern.finditer(text):
                    if rule.name == "credit_card" and not luhn_check(m.group()):
                        continue
                    matches.append(ScanMatch(rule_name=rule.name, start=m.start(), end=m.end(), replacement=""))
        return ScanResult(text=text, matches=matches, had_detections=len(matches) > 0)

    def apply(
        self,
        text: str,
        session_id: str = "unknown",
        source: str = "unknown",
        provider: str = "unknown",
        model: str = "",
    ) -> tuple[str, list[ScanMatch]]:
        self._maybe_reload()
        self._total_scans += 1
        if not self._enabled or not text or self.should_skip(provider, model):
            return text, []

        ctx = ActionContext(session_id=session_id, source=source, provider=provider)
        all_matches: list[tuple[int, int, str, ScanRule]] = []

        for rule in self._rules:
            if not rule.enabled:
                continue
            for pattern in rule.patterns:
                for m in pattern.finditer(text):
                    matched_text = m.group()
                    if rule.name == "credit_card" and not luhn_check(matched_text):
                        continue
                    all_matches.append((m.start(), m.end(), matched_text, rule))

        if not all_matches:
            return text, []

        all_matches.sort(key=lambda x: x[0], reverse=True)

        deduped = []
        occupied: set[int] = set()
        for start, end, matched_text, rule in all_matches:
            positions = set(range(start, end))
            if positions & occupied:
                continue
            occupied |= positions
            deduped.append((start, end, matched_text, rule))

        result_text = text
        scan_matches = []

        for start, end, matched_text, rule in deduped:
            replacement = apply_action(
                matched_text=matched_text, rule_name=rule.name,
                action=rule.action, mask_format=rule.mask_format, ctx=ctx,
            )
            if replacement != matched_text:
                result_text = result_text[:start] + replacement + result_text[end:]
            scan_matches.append(ScanMatch(rule_name=rule.name, start=start, end=end, replacement=replacement))
            self._total_detections += 1
            self._detections_by_rule[rule.name] = self._detections_by_rule.get(rule.name, 0) + 1

        if self._log_detections:
            self._write_detection_log(scan_matches, session_id, source, provider)

        return result_text, scan_matches

    def sanitize_log_entry(self, entry: dict) -> dict:
        if not self._enabled or not self._sanitize_logs:
            return entry
        return self._sanitize_value(entry)

    def _sanitize_value(self, value: Any) -> Any:
        if isinstance(value, str):
            if not value:
                return value
            for rule in self._rules:
                if not rule.enabled:
                    continue
                for pattern in rule.patterns:
                    for m in pattern.finditer(value):
                        matched = m.group()
                        if rule.name == "credit_card" and not luhn_check(matched):
                            continue
                        value = value.replace(matched, f"[REDACTED:{rule.name}]")
            return value
        if isinstance(value, dict):
            return {k: self._sanitize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._sanitize_value(item) for item in value]
        return value

    def has_buffer_rules(self) -> bool:
        return any(r.enabled and r.streaming_mode == "buffer" for r in self._rules)

    def rules_summary(self) -> list[dict]:
        self._maybe_reload()
        return [
            {
                "name": r.name,
                "description": r.description,
                "action": r.action,
                "enabled": r.enabled,
                "pattern_count": len(r.patterns),
                "streaming_mode": r.streaming_mode,
                "mask_format": r.mask_format,
                "detections": self._detections_by_rule.get(r.name, 0),
            }
            for r in self._rules
        ]

    def stats(self) -> dict:
        return {
            "total_scans": self._total_scans,
            "total_detections": self._total_detections,
            "by_rule": dict(self._detections_by_rule),
        }

    def skip_config(self) -> dict:
        return {
            "skip_providers": sorted(self._skip_providers),
            "skip_models": sorted(self._skip_models),
            "model_tags": self._model_tags,
            "trusted_tags": sorted(self._trusted_tags),
            "content_logging": self._content_logging,
            "sanitize_logs": self._sanitize_logs,
        }

    def update_rule(self, name: str, updates: dict) -> bool:
        for rule in self._rules:
            if rule.name == name:
                for key in ("enabled", "action", "mask_format", "streaming_mode"):
                    if key in updates:
                        setattr(rule, key, updates[key])
                        if key == "action" or key == "enabled":
                            # Re-apply to compiled patterns not needed; just attribute update
                            pass
                return True
        return False

    def _write_detection_log(
        self, matches: list[ScanMatch], session_id: str, source: str, provider: str,
    ) -> None:
        try:
            _DETECTIONS_DIR.mkdir(parents=True, exist_ok=True)
            log_path = _DETECTIONS_DIR / "scanner-detections.jsonl"
            for match in matches:
                entry = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "rule": match.rule_name,
                    "action": next((r.action for r in self._rules if r.name == match.rule_name), "unknown"),
                    "session_id": session_id,
                    "source": source,
                    "provider": provider,
                }
                with open(log_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
