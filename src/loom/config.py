"""Configuration models and loader for Loom.

Config is read from a YAML file (``loom.yaml`` by default) into a tree of Pydantic
models. The resolution order for :meth:`LoomConfig.load` is:

1. ``$LOOM_CONFIG`` if set
2. ``./loom.yaml``
3. ``/etc/loom/loom.yaml``

If no file is found, a default :class:`LoomConfig` is returned so the gateway can still
start (with an empty provider list).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    model_id: str
    display_name: str = ""
    tier: str = "economy"
    supports_tools: bool = False
    supports_json_mode: bool = False
    max_context_tokens: int = 8192
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0


class ProviderConfig(BaseModel):
    name: str
    api_base: str
    models: list[ModelConfig] = Field(default_factory=list)

    def get_model(self, model_id: str) -> Optional[ModelConfig]:
        for model in self.models:
            if model.model_id == model_id or model.display_name == model_id:
                return model
        return None


class SourcePolicy(BaseModel):
    minimum_tier: str = "economy"
    requires_tools: bool = False
    allowed_providers: list[str] = Field(
        default_factory=lambda: ["anthropic", "openai"]
    )
    budget_tier: Optional[str] = None
    pinned_model: Optional[str] = None
    compression_tier: Optional[str] = None


class RoutingConfig(BaseModel):
    default_determinism_target: float = 0.7
    min_empirical_runs: int = 10
    routing_table_path: str = ""
    reroute_enabled: bool = True
    programmatic_search_enabled: bool = True
    search_sources: dict[str, str] = Field(default_factory=dict)


class CompressionConfig(BaseModel):
    enabled: bool = True
    default_tier: str = "medium"


class StorageConfig(BaseModel):
    database_path: str = "loom.db"


class ObservabilityConfig(BaseModel):
    audit_log_path: str = "logs/audit.jsonl"
    metrics_log_path: str = "logs/metrics.jsonl"


class ScannerConfig(BaseModel):
    enabled: bool = False
    sanitize_logs: bool = True
    content_logging: str = "off"
    streaming_mode: str = "buffer"
    log_detections: bool = True
    rules_path: str = ""
    skip_providers: list[str] = Field(default_factory=list)
    skip_models: list[str] = Field(default_factory=list)
    model_tags: dict[str, list[str]] = Field(default_factory=dict)
    trusted_tags: list[str] = Field(default_factory=list)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 4000
    log_level: str = "info"
    display_timezone: str = "UTC"


class LoomConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)
    sources: dict[str, SourcePolicy] = Field(default_factory=dict)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)

    # ------------------------------------------------------------------ loaders
    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str]) -> "LoomConfig":
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        config = cls.model_validate(data)
        config._apply_env_overrides()
        return config

    @classmethod
    def load(cls) -> "LoomConfig":
        candidates: list[Path] = []
        env_path = os.environ.get("LOOM_CONFIG")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path("loom.yaml"))
        candidates.append(Path("/etc/loom/loom.yaml"))

        for candidate in candidates:
            if candidate.is_file():
                return cls.from_yaml(candidate)

        config = cls()
        config._apply_env_overrides()
        return config

    def _apply_env_overrides(self) -> None:
        """Apply a small set of LOOM_* environment overrides for server settings."""
        host = os.environ.get("LOOM_SERVER_HOST")
        if host:
            self.server.host = host
        port = os.environ.get("LOOM_SERVER_PORT")
        if port:
            try:
                self.server.port = int(port)
            except ValueError:
                pass
        log_level = os.environ.get("LOOM_SERVER_LOG_LEVEL")
        if log_level:
            self.server.log_level = log_level
        db_path = os.environ.get("LOOM_STORAGE_DATABASE_PATH")
        if db_path:
            self.storage.database_path = db_path

    # ------------------------------------------------------------------ accessors
    def get_provider(self, name: str) -> Optional[ProviderConfig]:
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None

    def get_source_policy(self, source: str) -> SourcePolicy:
        if source in self.sources:
            return self.sources[source]
        if "default" in self.sources:
            return self.sources["default"]
        return SourcePolicy()
