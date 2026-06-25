"""Routing data models and the EQRT recommendation algorithm.

Architecture: Eliminate -> Qualify -> Rank -> Tiebreak (EQRT)

  Step 0 (Pin):      A source with a ``pinned_model`` short-circuits routing.
  Step 1 (Eliminate): Hard gates remove disqualified candidates
                      (task type, allowed providers, tier floor, context
                      window, tool support, format compliance, budget cap).
  Step 2 (Qualify):   Empirically tested models (num_runs >= MIN_EMPIRICAL_RUNS)
                      are preferred over untested ones.
  Step 3 (Rank):      Lexicographic ranking — quality strictly before cost.
  Step 4 (Tiebreak):  Empirical confidence, then a small local/free bonus.

A :class:`RoutingTable` is normally generated from experimental data and
loaded from YAML. When no table is available the caller (the routing engine)
falls back to config-based selection.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .providers import ProviderRegistry, TIER_ORDER

# Minimum empirical runs before a model is considered "tested" for a task type.
# Below this threshold models are deprioritized (not eliminated) — they can
# still win if no tested models are available.
MIN_EMPIRICAL_RUNS = 10


@dataclass
class SourceProfile:
    """Per-source routing constraints, derived from a config ``SourcePolicy``."""

    name: str
    minimum_tier: str = "economy"
    requires_tools: bool = False
    allowed_providers: list[str] = field(default_factory=lambda: ["anthropic"])
    budget_tier: Optional[str] = None
    pinned_model: Optional[str] = None

    @classmethod
    def from_policy(cls, name: str, policy) -> "SourceProfile":
        return cls(
            name=name,
            minimum_tier=getattr(policy, "minimum_tier", "economy"),
            requires_tools=getattr(policy, "requires_tools", False),
            allowed_providers=list(
                getattr(policy, "allowed_providers", ["anthropic"])
            ),
            budget_tier=getattr(policy, "budget_tier", None),
            pinned_model=getattr(policy, "pinned_model", None),
        )


@dataclass
class RoutingEntry:
    """One model at one temperature for one task type."""

    model: str
    backend: str
    task_type: str
    temperature: float
    seed_strategy: str  # "none", "fixed", "warm_up"
    constraint_level_min: int
    determinism_score: float
    determinism_ci_lo: float
    determinism_ci_hi: float
    lexical_score: float
    structural_score: float
    semantic_score: float
    exact_match_pct: float
    avg_duration_ms: Optional[float] = None
    format_compliance_pct: Optional[float] = None
    num_runs: int = 0
    notes: str = ""


@dataclass
class RoutingRecommendation:
    """Output of a routing query."""

    model: str
    temperature: float
    seed_strategy: str
    constraint_level_min: int
    expected_determinism: float
    confidence_interval: tuple[float, float]
    notes: str = ""
    provider: str = ""
    engine: str = ""
    estimated_cost_per_1k: float = 0.0
    is_empirical: bool = True
    routing_reason: str = ""
    alternatives: list["RoutingRecommendation"] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "model": self.model,
            "temperature": self.temperature,
            "seed_strategy": self.seed_strategy,
            "constraint_level_min": self.constraint_level_min,
            "expected_determinism": self.expected_determinism,
            "confidence_interval": list(self.confidence_interval),
            "notes": self.notes,
            "provider": self.provider,
            "engine": self.engine,
            "estimated_cost_per_1k": self.estimated_cost_per_1k,
            "is_empirical": self.is_empirical,
            "routing_reason": self.routing_reason,
        }
        if self.alternatives:
            d["alternatives"] = [a.to_dict() for a in self.alternatives]
        return d


@dataclass
class RoutingTable:
    """Full routing table loaded from YAML or built from experiment data."""

    version: int = 1
    generated_at: str = ""
    generated_from: str = ""
    entries: list[RoutingEntry] = field(default_factory=list)

    # ------------------------------------------------------------------ I/O
    @classmethod
    def from_yaml(cls, path: Path | str) -> "RoutingTable":
        """Load a routing table from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f) or {}

        table = cls(
            version=data.get("version", 1),
            generated_at=data.get("generated_at", ""),
            generated_from=data.get("generated_from", ""),
        )

        for task_type, task_data in data.get("task_types", {}).items():
            for model, model_data in task_data.get("models", {}).items():
                backend = model_data.get("backend", _infer_provider(model))
                for tier in model_data.get("tiers", []):
                    det_score = tier.get("determinism", 0.0)
                    ci = tier.get("confidence_interval", [det_score, det_score])
                    table.entries.append(RoutingEntry(
                        model=model,
                        backend=backend,
                        task_type=task_type,
                        temperature=tier["temperature"],
                        seed_strategy=tier.get("seed_strategy", "none"),
                        constraint_level_min=tier.get("constraint_level_min", 1),
                        determinism_score=det_score,
                        determinism_ci_lo=ci[0],
                        determinism_ci_hi=ci[1],
                        lexical_score=tier.get("lexical", 0.0),
                        structural_score=tier.get("structural", 0.0),
                        semantic_score=tier.get("semantic", 0.0),
                        exact_match_pct=tier.get("exact_match_pct", 0.0),
                        avg_duration_ms=tier.get("avg_duration_ms"),
                        format_compliance_pct=tier.get("format_compliance_pct"),
                        num_runs=tier.get("num_runs", 0),
                        notes=tier.get("notes", ""),
                    ))
        return table

    def to_yaml(self, path: Path | str) -> None:
        """Write the routing table to a YAML file."""
        by_task: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for e in self.entries:
            tier = {
                "temperature": e.temperature,
                "seed_strategy": e.seed_strategy,
                "determinism": round(e.determinism_score, 4),
                "confidence_interval": [
                    round(e.determinism_ci_lo, 4),
                    round(e.determinism_ci_hi, 4),
                ],
                "lexical": round(e.lexical_score, 4),
                "structural": round(e.structural_score, 4),
                "semantic": round(e.semantic_score, 4),
                "exact_match_pct": round(e.exact_match_pct, 4),
                "constraint_level_min": e.constraint_level_min,
                "num_runs": e.num_runs,
            }
            if e.avg_duration_ms is not None:
                tier["avg_duration_ms"] = round(e.avg_duration_ms)
            if e.format_compliance_pct is not None:
                tier["format_compliance_pct"] = round(e.format_compliance_pct, 4)
            if e.notes:
                tier["notes"] = e.notes
            by_task[e.task_type][e.model].append(tier)

        output = {
            "version": self.version,
            "generated_at": self.generated_at,
            "generated_from": self.generated_from,
            "task_types": {},
        }
        for task_type in sorted(by_task):
            models_dict = {}
            for model in sorted(by_task[task_type]):
                tiers = sorted(
                    by_task[task_type][model],
                    key=lambda t: t["determinism"], reverse=True,
                )
                models_dict[model] = {
                    "backend": _infer_provider(model),
                    "tiers": tiers,
                }
            output["task_types"][task_type] = {"models": models_dict}

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(output, f, default_flow_style=False, sort_keys=False, width=120)

    # ------------------------------------------------------------------ EQRT
    def recommend(
        self,
        task_type: str,
        source_profile: SourceProfile,
        registry: Optional[ProviderRegistry] = None,
        requires_tools: bool = False,
        format_required: Optional[str] = None,
        payload_tokens_est: int = 0,
        determinism_target: float = 0.7,
    ) -> Optional[RoutingRecommendation]:
        """Recommend a model using Eliminate-Qualify-Rank-Tiebreak (EQRT).

        Quality is strictly prior to cost: a model must clear the quality bar
        before cost is even considered.
        """
        registry = registry or ProviderRegistry.default()
        profile = source_profile
        tools_needed = requires_tools or profile.requires_tools

        # -- STEP 0: PIN -- source-pinned model short-circuits routing --
        if profile.pinned_model:
            pinned = [
                e for e in self.entries
                if e.model == profile.pinned_model and e.temperature == 0.0
            ]
            if not pinned:
                pinned = [e for e in self.entries if e.model == profile.pinned_model]
            if pinned:
                return _build_recommendation(
                    pinned[0], registry,
                    notes="Pinned by source policy",
                    routing_reason=f"source_pin:{profile.name}->{profile.pinned_model}",
                )
            # Pinned model not in table — return it anyway via fallback.
            backend = _infer_provider(profile.pinned_model)
            return RoutingRecommendation(
                model=profile.pinned_model,
                temperature=0.0,
                seed_strategy="none",
                constraint_level_min=1,
                expected_determinism=0.0,
                confidence_interval=(0.0, 0.0),
                notes="Pinned by source policy (no routing table entry)",
                provider=backend,
                engine=backend,
                estimated_cost_per_1k=0.0,
                is_empirical=False,
                routing_reason=f"source_pin:{profile.name}->{profile.pinned_model}",
            )

        # -- STEP 1: ELIMINATE -- hard gates --
        candidates = self._filter_candidates(
            task_type=task_type,
            requires_tools=tools_needed,
            format_required=format_required,
            profile=profile,
            payload_tokens_est=payload_tokens_est,
            registry=registry,
        )
        if not candidates:
            return None

        # -- STEP 2: QUALIFY -- separate tested from untested --
        tested = [e for e in candidates if e.num_runs >= MIN_EMPIRICAL_RUNS]
        untested = [e for e in candidates if e.num_runs < MIN_EMPIRICAL_RUNS]
        ranking_pool = tested if tested else untested
        using_untested = not tested

        meeting_target = [
            e for e in ranking_pool if e.determinism_score >= determinism_target
        ]
        if not meeting_target:
            meeting_target = ranking_pool

        # -- STEP 3: RANK -- lexicographic: quality first, then cost --
        def rank_key(e: RoutingEntry) -> tuple:
            cost = registry.get_cost(e.model)
            cost_per_1k = cost if cost is not None else 0.0

            effective_det = e.determinism_score
            if e.num_runs == 0:
                effective_det = e.determinism_score * 0.7
            elif e.num_runs < 30:
                effective_det = e.determinism_score * (
                    0.7 + 0.3 * min(1.0, e.num_runs / 30)
                )

            if e.num_runs >= MIN_EMPIRICAL_RUNS:
                quality = min(effective_det, e.determinism_ci_lo)
            else:
                quality = effective_det

            if cost_per_1k > 0:
                cost_efficiency = quality / math.log2(cost_per_1k + 1)
            else:
                cost_efficiency = quality * 100  # free models get a big boost

            confidence = min(e.num_runs / 100, 1.0)
            local_bonus = 0.01 if e.backend == "ollama" else 0.0
            return (quality, cost_efficiency, confidence, local_bonus)

        ranked = sorted(meeting_target, key=rank_key, reverse=True)
        best = ranked[0]

        # -- STEP 4: BUILD RECOMMENDATION --
        reasons = []
        if using_untested:
            reasons.append("untested_pool")
        if best.task_type == "_default" and task_type != "_default":
            reasons.append(f"default_fallback(no {task_type} data)")
        if best.backend == "ollama":
            reasons.append("local_eligible")
        routing_reason = "|".join(reasons) if reasons else "eqrt_standard"

        fallback_note = ""
        if best.task_type == "_default" and task_type != "_default":
            fallback_note = f" (using _default data — run {task_type} experiments)"
        if using_untested:
            fallback_note += " (WARNING: no empirically tested models available)"

        rec = _build_recommendation(
            best, registry,
            notes=best.notes + fallback_note,
            routing_reason=routing_reason,
        )
        for alt in ranked[1:4]:
            rec.alternatives.append(_build_recommendation(alt, registry))
        return rec

    # ------------------------------------------------------------------ gates
    def _filter_candidates(
        self,
        task_type: str,
        requires_tools: bool,
        format_required: Optional[str],
        profile: SourceProfile,
        payload_tokens_est: int,
        registry: ProviderRegistry,
    ) -> list[RoutingEntry]:
        """Step 1: apply hard gates from cheapest to most expensive."""
        # Gate 1: Task type match (or _default fallback).
        candidates = [e for e in self.entries if e.task_type == task_type]
        if not candidates:
            candidates = [e for e in self.entries if e.task_type == "_default"]
        if not candidates:
            return []

        # Gate 2: Allowed providers.
        if profile.allowed_providers:
            allowed = set(profile.allowed_providers)
            filtered = [e for e in candidates if e.backend in allowed]
            if not filtered:
                default_allowed = [
                    e for e in self.entries
                    if e.task_type == "_default" and e.backend in allowed
                ]
                if default_allowed:
                    filtered = default_allowed
            if filtered:
                candidates = filtered

        # Gate 3: Minimum tier.
        min_order = TIER_ORDER.get(profile.minimum_tier, 0)
        if min_order > 0:
            def _meets_tier(e: RoutingEntry) -> bool:
                pm = registry.resolve(e.model)
                if pm is None:
                    return (min_order <= 0) if e.backend == "ollama" else (min_order <= 1)
                return TIER_ORDER.get(pm.tier, 0) >= min_order
            filtered = [e for e in candidates if _meets_tier(e)]
            if filtered:
                candidates = filtered

        # Gate 4: Context window fit (25% headroom for the response).
        if payload_tokens_est > 0:
            def _fits_context(e: RoutingEntry) -> bool:
                pm = registry.resolve(e.model)
                max_ctx = pm.max_context_tokens if pm else (
                    32768 if e.backend == "ollama" else 200000
                )
                return payload_tokens_est < max_ctx * 0.75
            filtered = [e for e in candidates if _fits_context(e)]
            if filtered:
                candidates = filtered

        # Gate 5: Tool support.
        if requires_tools:
            filtered = [
                e for e in candidates if _model_supports_tools(e.model, registry)
            ]
            if not filtered:
                default_tool = [
                    e for e in self.entries
                    if e.task_type == "_default"
                    and _model_supports_tools(e.model, registry)
                ]
                if default_tool:
                    filtered = default_tool
            if filtered:
                candidates = filtered

        # Gate 6: Format compliance.
        if format_required == "json":
            filtered = [
                e for e in candidates
                if e.format_compliance_pct is None or e.format_compliance_pct >= 0.8
            ]
            if filtered:
                candidates = filtered

        # Gate 7: Budget tier cap.
        if profile.budget_tier:
            max_order = TIER_ORDER.get(profile.budget_tier, 2)
            def _under_budget(e: RoutingEntry) -> bool:
                pm = registry.resolve(e.model)
                if pm is None:
                    return e.backend == "ollama"
                return TIER_ORDER.get(pm.tier, 0) <= max_order
            filtered = [e for e in candidates if _under_budget(e)]
            if filtered:
                candidates = filtered

        # Deduplicate: keep the best-determinism entry per model.
        best_per_model: dict[str, RoutingEntry] = {}
        for e in candidates:
            cur = best_per_model.get(e.model)
            if cur is None or e.determinism_score > cur.determinism_score:
                best_per_model[e.model] = e
        return list(best_per_model.values())


# --- Helpers ---

def _infer_provider(model: str) -> str:
    """Infer the provider/backend from a model name."""
    if model.startswith("openai:") or model.startswith("gpt-"):
        return "openai"
    if model.startswith("anthropic:") or model in ("opus", "sonnet", "haiku"):
        return "anthropic"
    if model.startswith("gemini"):
        return "gemini"
    return "ollama"


def _model_supports_tools(model: str, registry: ProviderRegistry) -> bool:
    pm = registry.resolve(model)
    if pm:
        return pm.supports_tools
    return _infer_provider(model) == "anthropic"


def _build_recommendation(
    entry: RoutingEntry,
    registry: ProviderRegistry,
    notes: str = "",
    routing_reason: str = "",
) -> RoutingRecommendation:
    """Build a recommendation from an entry, enriched with provider data."""
    pm = registry.resolve(entry.model)
    return RoutingRecommendation(
        model=entry.model,
        temperature=entry.temperature,
        seed_strategy=entry.seed_strategy,
        constraint_level_min=entry.constraint_level_min,
        expected_determinism=entry.determinism_score,
        confidence_interval=(entry.determinism_ci_lo, entry.determinism_ci_hi),
        notes=notes or entry.notes,
        provider=pm.provider if pm else entry.backend,
        engine=pm.engine if pm else entry.backend,
        estimated_cost_per_1k=pm.avg_cost_per_1k() if pm else 0.0,
        is_empirical=entry.num_runs > 0,
        routing_reason=routing_reason,
    )
