"""Catalog-backed token cost estimation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from theater.constants import MICROCENTS_PER_DOLLAR

_PRICES_PATH = Path(__file__).parent / "model_prices.json"
_PRICES: dict[str, dict] | None = None

# Maps provider-specific model names to catalog rows with matching public rates.
_ALIASES = {
    "openai-foundry/zai-glm-5-2": "cloudflare/@cf/zai-org/glm-5.2",
}


class TokenUsageLike(Protocol):
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    reasoning_output_tokens: int
    cost_usd: float | None


def _load() -> dict[str, dict]:
    global _PRICES  # noqa: PLW0603
    if _PRICES is None:
        with _PRICES_PATH.open() as file:
            _PRICES = json.load(file)
    return _PRICES


def _lookup(model: str, provider: str | None = None) -> dict | None:
    prices = _load()
    candidates = [model]
    if provider and "/" not in model:
        candidates.insert(0, f"{provider}/{model}")
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
    for candidate in candidates:
        if candidate in prices:
            return prices[candidate]
        alias = _ALIASES.get(candidate)
        if alias is not None and alias in prices:
            return prices[alias]
    return None


def estimate_cost_usd(
    model: str | None,
    *,
    provider: str | None = None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    reasoning_tokens: int,
) -> float | None:
    """Estimate token cost from the vendored model catalog."""
    if model is None:
        return None
    price = _lookup(model, provider)
    if price is None:
        return None
    input_rate = _rate(price, "input_cost_per_token")
    output_rate = _rate(price, "output_cost_per_token")
    cache_read_rate = _rate(price, "cache_read_input_token_cost", fallback=input_rate)
    cache_write_rate = _rate(price, "cache_creation_input_token_cost", fallback=input_rate)
    charges = (
        (input_tokens, input_rate),
        (output_tokens, output_rate),
        (cache_read_tokens, cache_read_rate),
        (cache_write_tokens, cache_write_rate),
        (reasoning_tokens, output_rate),
    )
    if any(tokens and rate is None for tokens, rate in charges):
        return None
    return float(sum(tokens * rate for tokens, rate in charges if rate is not None))


def _rate(price: dict, key: str, *, fallback: float | None = None) -> float | None:
    value = price.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def usage_cost_microcents(usage: TokenUsageLike) -> int:
    """Use a reported cost or estimate one for a normalized usage record."""
    if usage.cost_usd is not None:
        return round(usage.cost_usd * MICROCENTS_PER_DOLLAR)
    cost = estimate_cost_usd(
        usage.model,
        provider=getattr(usage, "provider", None),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens,
        cache_write_tokens=usage.cache_creation_input_tokens,
        reasoning_tokens=usage.reasoning_output_tokens,
    )
    return 0 if cost is None else round(cost * MICROCENTS_PER_DOLLAR)


__all__ = ["estimate_cost_usd", "usage_cost_microcents"]
