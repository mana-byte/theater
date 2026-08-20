"""Token cost calculation."""

from __future__ import annotations

from theater.harness.base import TokenUsage

MICROCENTS_PER_DOLLAR = 100_000_000


def usage_cost_microcents(usage: TokenUsage) -> int:
    """Cost in microcents (USD * 100_000_000) for a single turn's usage."""
    if usage.cost_usd is not None:
        return round(usage.cost_usd * MICROCENTS_PER_DOLLAR)
    return 0
