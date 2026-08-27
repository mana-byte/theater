"""Claude native usage conversion."""

from __future__ import annotations

from theater.harness.base import TokenUsage
from theater.trajectory.enums import CostProvenance
from theater.trajectory.records import TrajectoryUsage

from .timing import _claude_request_id
from .values import _trajectory_float, _trajectory_id, _trajectory_int


def _token_usage(message: dict, record: dict) -> TokenUsage | None:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    model = message.get("model")
    if not isinstance(model, str) or not model:
        model = None
    cost = record.get("costUSD")
    cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
    provider = message.get("provider") or record.get("provider")
    native_id = message.get("id") or record.get("requestId")
    usage_key = f"claude:{native_id}" if isinstance(native_id, str) and native_id else None
    return TokenUsage(
        model=model,
        provider=provider if isinstance(provider, str) and provider else None,
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_creation_input_tokens=int(raw.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(raw.get("cache_read_input_tokens") or 0),
        cost_usd=cost,
        cost_provenance=(CostProvenance.REPORTED if cost is not None else CostProvenance.UNKNOWN),
        idempotency_key=usage_key,
    )


def _claude_trajectory_usage(message: dict, record: dict) -> TrajectoryUsage | None:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    model = _trajectory_id(message.get("model"))
    provider = _trajectory_id(message.get("provider") or record.get("provider"))
    request_id = _claude_request_id(message, record)
    cost = _trajectory_float(record.get("costUSD"))
    return TrajectoryUsage(
        model=model,
        provider=provider,
        request_id=request_id,
        input_tokens=_trajectory_int(raw.get("input_tokens")),
        output_tokens=_trajectory_int(raw.get("output_tokens")),
        reasoning_tokens=_trajectory_int(raw.get("reasoning_output_tokens")),
        cache_read_tokens=_trajectory_int(raw.get("cache_read_input_tokens")),
        cache_write_tokens=_trajectory_int(raw.get("cache_creation_input_tokens")),
        cost_usd=cost if cost is None or cost >= 0 else None,
        cost_provenance=(
            CostProvenance.REPORTED if cost is not None and cost >= 0 else CostProvenance.UNKNOWN
        ),
    )
