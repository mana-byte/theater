"""Shared token usage and cost provenance normalization."""

from __future__ import annotations

import math
from typing import Protocol

from theater.harness.contracts.events import TokenUsage
from theater.trajectory.enums import CostProvenance
from theater.trajectory.records import TrajectoryUsage


class _IdentifierCallable(Protocol):
    def __call__(self, value: object) -> str | None: ...


def trajectory_usage_from_token_usage(
    usage: TokenUsage,
    *,
    identifier: _IdentifierCallable | None = None,
    validate_cost: bool = False,
    clamp_tokens: bool = False,
    force_unknown_cost: bool = False,
) -> TrajectoryUsage:
    """Convert a TokenUsage to a TrajectoryUsage.

    Defaults preserve values as reported. Callers opt into cost validation,
    token clamping, and unknown-cost provenance explicitly.
    """
    model = usage.model
    provider = usage.provider
    request_id = usage.idempotency_key
    if identifier is not None:
        model = identifier(model)
        provider = identifier(provider)
        request_id = identifier(request_id)
    cost = usage.cost_usd
    if (
        validate_cost
        and cost is not None
        and (not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0)
    ):
        cost = None
    provenance = usage.cost_provenance
    if force_unknown_cost and cost is None:
        provenance = CostProvenance.UNKNOWN
    if clamp_tokens:
        tokens = (
            max(0, usage.input_tokens),
            max(0, usage.output_tokens),
            max(0, usage.reasoning_output_tokens),
            max(0, usage.cache_read_input_tokens),
            max(0, usage.cache_creation_input_tokens),
        )
    else:
        tokens = (
            usage.input_tokens,
            usage.output_tokens,
            usage.reasoning_output_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
        )
    return TrajectoryUsage(
        model=model,
        provider=provider,
        request_id=request_id,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        reasoning_tokens=tokens[2],
        cache_read_tokens=tokens[3],
        cache_write_tokens=tokens[4],
        cost_usd=cost,
        cost_provenance=provenance,
    )


def reported_cost(value: object, *, strict_positive: bool) -> tuple[float | None, CostProvenance]:
    """Extract a reported cost and its provenance.

    When *strict_positive* is true, zero means "no price given" and is dropped.
    Otherwise exact zero is accepted.
    """
    if not isinstance(value, (int, float)):
        return None, CostProvenance.UNKNOWN
    cost = float(value)
    if not math.isfinite(cost) or cost < 0:
        return None, CostProvenance.UNKNOWN
    if strict_positive and cost == 0:
        return None, CostProvenance.UNKNOWN
    return cost, CostProvenance.REPORTED


def qualified_model(provider: str | None, model: str | None) -> str | None:
    """Join provider and model with a slash, or return whichever is present."""
    if isinstance(provider, str) and isinstance(model, str) and provider and model:
        return f"{provider}/{model}"
    if isinstance(model, str) and model:
        return model
    return None
