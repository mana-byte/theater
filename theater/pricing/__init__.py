"""Token cost calculation using vendored LiteLLM pricing data."""

from __future__ import annotations

import json
from pathlib import Path

from theater.harness.base import TokenUsage

MICROCENTS_PER_DOLLAR = 100_000_000

_PRICES_PATH = Path(__file__).parent / "model_prices.json"
_PRICES: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _PRICES  # noqa: PLW0603
    if _PRICES is None:
        with _PRICES_PATH.open() as f:
            _PRICES = json.load(f)
    return _PRICES


def _lookup(model: str) -> dict | None:
    prices = _load()
    if model in prices:
        return prices[model]
    if "/" in model:
        bare = model.split("/", 1)[1]
        if bare in prices:
            return prices[bare]
    return None


def usage_cost_microcents(usage: TokenUsage) -> int:
    """Cost in microcents (USD * 100_000_000) for a single turn's usage."""
    if usage.cost_usd is not None:
        return round(usage.cost_usd * MICROCENTS_PER_DOLLAR)
    if usage.model is None:
        return 0
    p = _lookup(usage.model)
    if p is None:
        return 0
    cost = (
        usage.input_tokens * p.get("input_cost_per_token", 0)
        + usage.output_tokens * p.get("output_cost_per_token", 0)
        + usage.cache_read_input_tokens * p.get("cache_read_input_token_cost", 0)
        + usage.cache_creation_input_tokens * p.get("cache_creation_input_token_cost", 0)
        + usage.reasoning_output_tokens * p.get("output_cost_per_token", 0)
    )
    return round(cost * MICROCENTS_PER_DOLLAR)
