"""Provider registry for LLM routing.

Defines model capabilities, pricing, and tiers for the providers Loom knows
about. The registry can be built from a :class:`~loom.config.LoomConfig` or
fall back to a small set of hardcoded defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


TIER_ORDER = {"economy": 0, "standard": 1, "premium": 2}


@dataclass
class ProviderModel:
    """A single model available for routing."""

    provider: str          # anthropic, openai, ollama, gemini
    model_id: str          # claude-sonnet-4-6, gpt-4o-mini, qwen2.5:7b
    display_name: str      # short, router-facing name
    engine: str            # provider adapter name
    supports_tools: bool
    supports_json_mode: bool
    cost_per_1k_input: float
    cost_per_1k_output: float
    max_context_tokens: int
    tier: str              # economy, standard, premium

    def avg_cost_per_1k(self) -> float:
        """Blended cost assuming a 1:1 input:output ratio."""
        return (self.cost_per_1k_input + self.cost_per_1k_output) / 2

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "engine": self.engine,
            "supports_tools": self.supports_tools,
            "supports_json_mode": self.supports_json_mode,
            "cost_per_1k_input": self.cost_per_1k_input,
            "cost_per_1k_output": self.cost_per_1k_output,
            "max_context_tokens": self.max_context_tokens,
            "tier": self.tier,
        }


@dataclass
class ProviderRegistry:
    """Collection of available models, keyed by display_name."""

    models: dict[str, ProviderModel] = field(default_factory=dict)

    def resolve(self, name: str) -> Optional[ProviderModel]:
        """Look up a model by display_name, model_id, or prefixed name.

        Examples that all resolve to the same model: ``sonnet``,
        ``claude-sonnet-4-6``, ``anthropic:claude-sonnet-4-6``.
        """
        if name in self.models:
            return self.models[name]
        unprefixed = name
        if ":" in name:
            unprefixed = name.split(":", 1)[1]
            if unprefixed in self.models:
                return self.models[unprefixed]
        for m in self.models.values():
            if m.model_id == name or m.model_id == unprefixed:
                return m
        return None

    def filter_by_tier(self, max_tier: str) -> list[ProviderModel]:
        """Return models at or below the given tier."""
        max_order = TIER_ORDER.get(max_tier, 2)
        return [
            m for m in self.models.values()
            if TIER_ORDER.get(m.tier, 0) <= max_order
        ]

    def filter_by_capability(self, requires_tools: bool = False) -> list[ProviderModel]:
        """Filter models by capability requirements."""
        if requires_tools:
            return [m for m in self.models.values() if m.supports_tools]
        return list(self.models.values())

    def get_cost(self, display_name: str) -> Optional[float]:
        """Blended cost per 1k tokens for a model, or None if unknown."""
        m = self.resolve(display_name)
        return m.avg_cost_per_1k() if m else None

    def to_dict(self) -> dict:
        return {name: m.to_dict() for name, m in self.models.items()}

    # ------------------------------------------------------------------ loaders
    @classmethod
    def from_dict(cls, data: dict) -> "ProviderRegistry":
        """Build a registry from a plain dict keyed by display_name."""
        models = {}
        for name, d in data.items():
            models[name] = ProviderModel(
                provider=d["provider"],
                model_id=d["model_id"],
                display_name=d.get("display_name", name),
                engine=d.get("engine", d["provider"]),
                supports_tools=d.get("supports_tools", False),
                supports_json_mode=d.get("supports_json_mode", False),
                cost_per_1k_input=d.get("cost_per_1k_input", 0.0),
                cost_per_1k_output=d.get("cost_per_1k_output", 0.0),
                max_context_tokens=d.get("max_context_tokens", 8192),
                tier=d.get("tier", "standard"),
            )
        return cls(models=models)

    @classmethod
    def from_config(cls, config) -> "ProviderRegistry":
        """Build a registry from a :class:`~loom.config.LoomConfig`.

        Each model's ``engine`` is set to its provider name. Falls back to the
        hardcoded defaults if the config declares no providers/models.
        """
        models: dict[str, ProviderModel] = {}
        for prov in config.providers:
            for m in prov.models:
                name = m.display_name or m.model_id
                models[name] = ProviderModel(
                    provider=prov.name,
                    model_id=m.model_id,
                    display_name=name,
                    engine=prov.name,
                    supports_tools=m.supports_tools,
                    supports_json_mode=m.supports_json_mode,
                    cost_per_1k_input=m.cost_per_1k_input,
                    cost_per_1k_output=m.cost_per_1k_output,
                    max_context_tokens=m.max_context_tokens,
                    tier=m.tier,
                )
        if not models:
            return cls.default()
        return cls(models=models)

    @classmethod
    def default(cls) -> "ProviderRegistry":
        """A small set of sensible defaults across the common providers."""
        return cls.from_dict({
            "fable": {
                "provider": "anthropic",
                "model_id": "claude-fable-5",
                "display_name": "fable",
                "engine": "anthropic",
                "supports_tools": True,
                "supports_json_mode": True,
                "cost_per_1k_input": 0.010,
                "cost_per_1k_output": 0.050,
                "max_context_tokens": 200000,
                "tier": "premium",
            },
            "opus": {
                "provider": "anthropic",
                "model_id": "claude-opus-4-8",
                "display_name": "opus",
                "engine": "anthropic",
                "supports_tools": True,
                "supports_json_mode": True,
                "cost_per_1k_input": 0.005,
                "cost_per_1k_output": 0.025,
                "max_context_tokens": 1000000,
                "tier": "premium",
            },
            "sonnet-5": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-5",
                "display_name": "sonnet-5",
                "engine": "anthropic",
                "supports_tools": True,
                "supports_json_mode": True,
                "cost_per_1k_input": 0.002,
                "cost_per_1k_output": 0.010,
                "max_context_tokens": 1000000,
                "tier": "standard",
            },
            "sonnet": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-6",
                "display_name": "sonnet",
                "engine": "anthropic",
                "supports_tools": True,
                "supports_json_mode": True,
                "cost_per_1k_input": 0.003,
                "cost_per_1k_output": 0.015,
                "max_context_tokens": 1000000,
                "tier": "standard",
            },
            "haiku": {
                "provider": "anthropic",
                "model_id": "claude-haiku-4-5-20251001",
                "display_name": "haiku",
                "engine": "anthropic",
                "supports_tools": True,
                "supports_json_mode": True,
                "cost_per_1k_input": 0.001,
                "cost_per_1k_output": 0.005,
                "max_context_tokens": 200000,
                "tier": "economy",
            },
            "gpt-4o-mini": {
                "provider": "openai",
                "model_id": "gpt-4o-mini",
                "display_name": "gpt-4o-mini",
                "engine": "openai",
                "supports_tools": True,
                "supports_json_mode": True,
                "cost_per_1k_input": 0.00015,
                "cost_per_1k_output": 0.0006,
                "max_context_tokens": 128000,
                "tier": "economy",
            },
            "qwen2.5:7b": {
                "provider": "ollama",
                "model_id": "qwen2.5:7b",
                "display_name": "qwen2.5:7b",
                "engine": "ollama",
                "supports_tools": False,
                "supports_json_mode": False,
                "cost_per_1k_input": 0.0,
                "cost_per_1k_output": 0.0,
                "max_context_tokens": 32768,
                "tier": "economy",
            },
        })
