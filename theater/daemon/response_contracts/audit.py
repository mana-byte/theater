"""Internal audit of strict-contract failures: a durable row plus a bounded counter."""

from __future__ import annotations

import contextlib
from typing import Any

from theater.constants.observability import RESPONSE_CONTRACT_FAILURE_METRIC
from theater.models import Job, now
from theater.observability.engine import metric_bridge
from theater.observability.metrics import MetricKind, MetricSpec

REPAIR_STARTED = "repair_started"
REPAIR_SUCCEEDED = "repair_succeeded"
UNSUPPORTED_ROUTE = "unsupported_route"
CONTRACT_UNAVAILABLE = "contract_unavailable"
REPAIR_BLOCKED = "repair_blocked"
REPAIR_REJECTED = "repair_rejected"
REPAIR_UNKNOWN = "repair_unknown"
REPAIR_TURN_FAILED = "repair_turn_failed"
REPAIR_TURN_CONFLICT = "repair_turn_conflict"
REPAIR_EXHAUSTED = "repair_exhausted"
REPAIR_TIMEOUT = "repair_timeout"
REPAIR_INTERRUPTED = "repair_interrupted"

#: Events that mean the caller got ``unable_to_parse``; only these are counted.
FAILURE_EVENTS = frozenset(
    {
        UNSUPPORTED_ROUTE,
        CONTRACT_UNAVAILABLE,
        REPAIR_BLOCKED,
        REPAIR_REJECTED,
        REPAIR_UNKNOWN,
        REPAIR_TURN_FAILED,
        REPAIR_TURN_CONFLICT,
        REPAIR_EXHAUSTED,
        REPAIR_TIMEOUT,
    }
)

_FAILURE_SPEC = MetricSpec(
    RESPONSE_CONTRACT_FAILURE_METRIC,
    "Strict response contracts that ended unable_to_parse, by internal reason.",
    "1",
    MetricKind.COUNTER,
    ("reason",),
)


def register_metric_specs() -> None:
    with contextlib.suppress(Exception):
        bridge = metric_bridge()
        if bridge is not None:
            bridge.register_specs((_FAILURE_SPEC,))


def record(store: Any, job: Job, event: str, detail: str | None = None, *, connection=None) -> None:
    """Append one audit row for ``job``; failures also tick the bounded counter."""
    store.record_contract_audit(
        job.handle, job.target_id, event, detail, at=now(), connection=connection
    )
    if event in FAILURE_EVENTS:
        with contextlib.suppress(Exception):
            bridge = metric_bridge()
            if bridge is not None:
                bridge.observe(_FAILURE_SPEC, 1, {"reason": event})
