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
from .providers import ProviderModel, ProviderRegistry, TIER_ORDER


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

    @property
    def table(self) -> Optional[RoutingTable]:
        return self._table

    def recommend(
        self,
        task_type: str,
        source: str = "default",
        requires_tools: bool = False,
        format_required: Optional[str] = None,
        payload_tokens_est: int = 0,
        min_tier_floor: Optional[str] = None,
        preferred_model: Optional[str] = None,
    ) -> Optional[RoutingRecommendation]:
        """Recommend a model using the EQRT algorithm, or config fallback.

        ``min_tier_floor``, when higher than the source's configured
        ``minimum_tier``, raises the floor for this call only — it never
        lowers it. The caller (the gateway) is responsible for deciding
        when a floor applies; a source's own policy always wins on a pin.

        ``preferred_model`` is an *input*, not a bypass: a caller (e.g. a
        Pulse task's ``model-preference:`` tag, forwarded by the executor)
        can name a model it would like, and this is honored only when that
        model passes the same policy every other candidate must pass —
        allowed providers, tier floor/budget, and tool support. When it
        can't be honored, routing proceeds exactly as it would with no
        preference, and the returned recommendation's ``routing_reason``
        notes the override so it's visible in logs/observability.
        """
        policy = self._config.get_source_policy(source)
        profile = SourceProfile.from_policy(source, policy)

        if min_tier_floor and TIER_ORDER.get(min_tier_floor, -1) > TIER_ORDER.get(
            profile.minimum_tier, 0
        ):
            profile.minimum_tier = min_tier_floor

        if preferred_model:
            honored = self._honor_preferred(preferred_model, profile, requires_tools)
            if honored is not None:
                return honored

        if self._table:
            rec = self._table.recommend(
                task_type=task_type,
                source_profile=profile,
                registry=self._registry,
                requires_tools=requires_tools,
                format_required=format_required,
                payload_tokens_est=payload_tokens_est,
            )
        else:
            rec = self._config_fallback(profile, requires_tools)

        if preferred_model and rec is not None:
            rec.routing_reason = (
                f"{rec.routing_reason}|preferred_overridden:{preferred_model}"
                if rec.routing_reason
                else f"preferred_overridden:{preferred_model}"
            )
        return rec

    def _passes_policy(
        self,
        model: ProviderModel,
        provider: str,
        profile: SourceProfile,
        tools_needed: bool,
    ) -> bool:
        """Shared eligibility check: allowed providers, eligible models,
        tier within [minimum_tier, budget_tier], and tool support."""
        allowed = set(profile.allowed_providers) if profile.allowed_providers else None
        if allowed is not None and provider not in allowed:
            return False
        eligible = set(profile.eligible_models) if profile.eligible_models else None
        if eligible is not None and model.model_id not in eligible:
            return False
        min_order = TIER_ORDER.get(profile.minimum_tier, 0)
        budget_order = (
            TIER_ORDER.get(profile.budget_tier, 2) if profile.budget_tier else 2
        )
        tier_order = TIER_ORDER.get(model.tier, 0)
        if tier_order < min_order or tier_order > budget_order:
            return False
        if tools_needed and not model.supports_tools:
            return False
        return True

    def _honor_preferred(
        self,
        preferred_model: str,
        profile: SourceProfile,
        requires_tools: bool = False,
    ) -> Optional[RoutingRecommendation]:
        """Return a recommendation for ``preferred_model`` if — and only
        if — it resolves to a known model that passes the source's policy.
        Returns ``None`` (never raises) when it can't be honored, letting
        the caller fall through to normal routing.
        """
        resolved = self._registry.resolve(preferred_model)
        if resolved is None:
            return None
        tools_needed = requires_tools or profile.requires_tools
        if not self._passes_policy(resolved, resolved.provider, profile, tools_needed):
            return None
        return RoutingRecommendation(
            model=resolved.model_id,
            temperature=0.0,
            seed_strategy="none",
            constraint_level_min=1,
            expected_determinism=0.0,
            confidence_interval=(0.0, 0.0),
            provider=resolved.provider,
            engine=resolved.engine,
            estimated_cost_per_1k=resolved.avg_cost_per_1k(),
            is_empirical=False,
            routing_reason="preferred_honored",
        )

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
        candidates = []
        for prov in self._config.providers:
            for m in prov.models:
                if not self._passes_policy(m, prov.name, profile, tools_needed):
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
