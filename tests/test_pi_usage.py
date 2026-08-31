"""Pi usage normalization at the durable accounting boundary."""

from __future__ import annotations

import json
from pathlib import Path

from theater.daemon.persistence.store import Store
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.contracts.events import TokenUsage
from theater.models import Participant
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


def test_duplicate_usage_repairs_only_a_previously_missing_cost(tmp_path: Path) -> None:
    store = Store(tmp_path / "theater.db")
    participant = Participant(id="pi-agent", harness="pi")
    store.upsert_participant(participant)
    values = {
        "participant_id": participant.id,
        "tree_root_id": participant.id,
        "usage_key": "native-session:assistant-1",
        "ts": 1.0,
        "model": "zai-glm-5-2",
        "harness": "pi",
        "input_tokens": 1_000,
        "output_tokens": 100,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 200,
        "reasoning_output_tokens": 0,
    }
    try:
        assert store.record_usage(**values, cost_microcents=0) is True
        assert store.record_usage(**values, cost_microcents=123_456) is False
        totals = store.usage_totals()
        assert totals["input_tokens"] == 1_000
        assert totals["cost_microcents"] == 123_456
    finally:
        store.close()
