"""Per-model token prices used only for cost estimates.

Routing reads prices from the provider model list in ``loom.yaml``. This table is
separate on purpose: a model a client asks for by name (for example Claude Code
sending ``claude-opus-5-5``) is proxied even when it is not in that list, and
it still needs a price. Adding it to the provider list instead would make it a
routing candidate, which changes which model Loom picks for routed traffic.

Prices are USD per 1K tokens. ``cache_read`` is the price of a cache hit; cache
writes are billed at the input price times 1.25 (5-minute TTL) or 2.0 (1-hour
TTL). Built-in defaults cover current Claude models; ``pricing:`` in
``loom.yaml`` adds or overrides entries.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel

CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
DEFAULT_CACHE_READ_MULTIPLIER = 0.1


class ModelPrice(BaseModel):
    input: float
    output: float
    cache_read: Optional[float] = None  # None = 0.1x input


# Anthropic list prices, per 1K tokens. Source: Anthropic model docs, 2026-11.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice(input=0.010, output=0.050, cache_read=0.00025),
    "claude-mythos-5-1": ModelPrice(input=0.010, output=0.050, cache_read=0.00025),
    "claude-fable-5": ModelPrice(input=0.010, output=0.050, cache_read=0.001),
    "claude-mythos-5": ModelPrice(input=0.010, output=0.050, cache_read=0.001),
    "claude-opus-5-5": ModelPrice(input=0.004, output=0.020, cache_read=0.0002),
    "claude-opus-5": ModelPrice(input=0.005, output=0.025),
    "claude-opus-4-8": ModelPrice(input=0.005, output=0.025),
    "claude-opus-4-7": ModelPrice(input=0.005, output=0.025),
    "claude-opus-4-6": ModelPrice(input=0.005, output=0.025),
    "claude-sonnet-5-5": ModelPrice(input=0.002, output=0.010, cache_read=0.0002),
    "claude-sonnet-5": ModelPrice(input=0.002, output=0.010),
    "claude-sonnet-4-6": ModelPrice(input=0.003, output=0.015),
    "claude-sonnet-4-5": ModelPrice(input=0.003, output=0.015),
    # Haiku 5.5 is $0.50/$2.50 per MTok above 100K prompt tokens; the base rate
    # is used here, so very long Haiku 5.5 prompts are under-counted.
    "claude-haiku-5-5": ModelPrice(input=0.0001, output=0.0005, cache_read=0.00001),
    "claude-haiku-4-5": ModelPrice(input=0.001, output=0.005),
}

_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")


def _base_id(model_id: str) -> str:
    """``claude-haiku-4-5-20251001`` -> ``claude-haiku-4-5``."""
    return _SNAPSHOT_SUFFIX.sub("", model_id.strip().lower())


_overrides: dict[str, ModelPrice] = {}


def set_overrides(overrides: Optional[dict[str, ModelPrice]]) -> None:
    """Install the ``pricing:`` section of ``loom.yaml`` (called at config load)."""
    _overrides.clear()
    _overrides.update({k.lower(): v for k, v in (overrides or {}).items()})


def estimate_cost(
    price: ModelPrice,
    tokens_in: int,
    tokens_out: int,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
) -> float:
    """USD cost of one request. ``tokens_in`` includes the cache tokens."""
    cache_read_price = (
        price.cache_read
        if price.cache_read is not None
        else price.input * DEFAULT_CACHE_READ_MULTIPLIER
    )
    base_input = max(0, tokens_in - cache_read - cache_write_5m - cache_write_1h)
    return (
        base_input * price.input
        + cache_read * cache_read_price
        + cache_write_5m * price.input * CACHE_WRITE_5M_MULTIPLIER
        + cache_write_1h * price.input * CACHE_WRITE_1H_MULTIPLIER
        + tokens_out * price.output
    ) / 1000.0


def lookup_price(
    model_id: Optional[str], overrides: Optional[dict[str, ModelPrice]] = None
) -> Optional[ModelPrice]:
    """Price for ``model_id``: exact id first, then the id without a date suffix.

    ``overrides`` (from ``loom.yaml``) win over the built-in defaults.
    """
    if not model_id:
        return None
    table: dict[str, ModelPrice] = {k.lower(): v for k, v in DEFAULT_PRICES.items()}
    for k, v in (overrides if overrides is not None else _overrides).items():
        table[k.lower()] = v
    key = model_id.strip().lower()
    if key in table:
        return table[key]
    base = _base_id(key)
    if base in table:
        return table[base]
    # A table entry may itself carry a date suffix.
    for k, v in table.items():
        if _base_id(k) == base:
            return v
    return None
