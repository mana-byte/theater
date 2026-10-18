"""Pi usage normalization at the durable accounting boundary."""

from __future__ import annotations

import json

from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.contracts.events import TokenUsage
from theater.pricing import usage_cost_microcents
from theater.trajectory.enums import CostProvenance


def _assistant_usage(*, cost: float) -> TokenUsage:
    parsed = PiObserver().parse_record(
        json.dumps(
            {
                "type": "message",
                "id": "assistant-1",
                "timestamp": "2026-08-31T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "provider": "mistral",
                    "model": "zai-glm-5-2",
                    "content": [{"type": "text", "text": "done"}],
                    "stopReason": "stop",
                    "usage": {
                        "input": 1_000,
                        "output": 100,
                        "cacheRead": 200,
                        "cacheWrite": 300,
                        "cost": {"total": cost},
                    },
                },
            }
        ),
        1,
    )
    return next(event.usage for event in parsed.events if event.usage is not None)


def test_pi_zero_placeholder_cost_falls_back_to_catalog_estimate() -> None:
    usage = _assistant_usage(cost=0)

    assert usage.cost_usd is None
    assert usage.cost_provenance is CostProvenance.UNKNOWN
    assert usage_cost_microcents(usage) > 0


def test_pi_positive_reported_cost_remains_authoritative() -> None:
    usage = _assistant_usage(cost=0.25)

    assert usage.cost_usd == 0.25
    assert usage.cost_provenance is CostProvenance.REPORTED
    assert usage_cost_microcents(usage) == 25_000_000
