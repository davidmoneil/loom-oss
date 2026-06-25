"""Routing engine — the public API over the routing table and provider registry.

The engine loads an optional empirical routing table (YAML). When present,
recommendations come from the EQRT algorithm. When absent, it falls back to
config-based selection: the cheapest model that satisfies the source policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import LoomConfig
from .models import RoutingRecommendation, RoutingTable, SourceProfile
from .providers import ProviderRegistry, TIER_ORDER


class RoutingEngine:
    """Wraps the routing table and provides a clean recommendation API."""

    def __init__(self, config: LoomConfig):
        self._config = config
        self._registry = ProviderRegistry.from_config(config)
        self._table: Optional[RoutingTable] = None
        self._table_path = config.routing.routing_table_path
        self._load_table()

    def _load_table(self) -> None:
        """Load the routing table from YAML if one is configured and present."""
        if self._table_path:
            path = Path(self._table_path)
            if path.exists():
                self._table = RoutingTable.from_yaml(path)

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def has_table(self) -> bool:
        return self._table is not None

    def recommend(
        self,
        task_type: str,
        source: str = "default",
        requires_tools: bool = False,
        format_required: Optional[str] = None,
        payload_tokens_est: int = 0,
    ) -> Optional[RoutingRecommendation]:
        """Recommend a model using the EQRT algorithm, or config fallback."""
        policy = self._config.get_source_policy(source)
        profile = SourceProfile.from_policy(source, policy)

        if self._table:
            return self._table.recommend(
                task_type=task_type,
                source_profile=profile,
                registry=self._registry,
                requires_tools=requires_tools,
                format_required=format_required,
                payload_tokens_est=payload_tokens_est,
            )
        return self._config_fallback(profile, requires_tools)

    def _config_fallback(
        self,
        profile: SourceProfile,
        requires_tools: bool = False,
    ) -> Optional[RoutingRecommendation]:
        """Pick a model from config when no routing table exists.

        Honors allowed_providers, minimum_tier, tool support, and budget cap,
        then chooses the cheapest qualifying model.
        """
        tools_needed = requires_tools or profile.requires_tools
        min_order = TIER_ORDER.get(profile.minimum_tier, 0)
        budget_order = (
            TIER_ORDER.get(profile.budget_tier, 2)
            if profile.budget_tier else 2
        )
        allowed = set(profile.allowed_providers) if profile.allowed_providers else None

        candidates = []
        for prov in self._config.providers:
            if allowed is not None and prov.name not in allowed:
                continue
            for m in prov.models:
                tier_order = TIER_ORDER.get(m.tier, 0)
                if tier_order < min_order or tier_order > budget_order:
                    continue
                if tools_needed and not m.supports_tools:
                    continue
                candidates.append((prov.name, m))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[1].cost_per_1k_input + x[1].cost_per_1k_output)
        provider, model = candidates[0]
        return RoutingRecommendation(
            model=model.model_id,
            temperature=0.0,
            seed_strategy="none",
            constraint_level_min=1,
            expected_determinism=0.0,
            confidence_interval=(0.0, 0.0),
            provider=provider,
            engine=provider,
            estimated_cost_per_1k=model.cost_per_1k_input,
            is_empirical=False,
            routing_reason="config_fallback",
        )
