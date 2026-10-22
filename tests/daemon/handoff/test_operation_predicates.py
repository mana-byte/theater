"""Delivery questions have different answers at the same operation edge."""

from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from tests.support.builders import make_job, make_participant
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.operation_predicates import (
    accepted_for_play_assignment,
    cancel_accepted,
    cancel_delivery_started,
    cancel_outcome_definitive,
    may_have_reached_target,
    names_exact_native_prompt_turn,
    proven_unsent,
    started_task,
)
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase as Phase,
)
from theater.harness.contracts.runtime import (
    ControlKind,
    ControlTransport,
)
from theater.harness.contracts.runtime import (
    DeliveryResult as Result,
)


def _operation(**changes) -> ControlOperation:
    base = ControlOperation(
        operation_id="op",
        participant_id="p1",
        kind=ControlKind.SEND,
        transport=ControlTransport.NATIVE_RUNTIME,
        delivery_phase=Phase.QUEUED,
        created_at=1.0,
        updated_at=1.0,
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    (
        "phase",
        "result",
        "turn",
        "barrier",
        "may_reach",
        "started",
        "cancelled",
        "definitive",
        "accepted",
    ),
    [
        (Phase.QUEUED, None, None, False, False, False, False, False, False),
        (Phase.QUEUED, Result.ACCEPTED, None, False, False, True, False, False, False),
        (Phase.DISPATCHED, None, None, False, True, True, True, False, False),
        (Phase.SETTLED, Result.REJECTED, None, False, False, False, True, True, False),
        (Phase.SETTLED, Result.REJECTED, "turn", False, False, True, True, True, False),
        (Phase.SETTLED, Result.ACCEPTED, None, False, True, True, True, True, True),
        (Phase.SETTLED, Result.ACCEPTED, None, True, True, True, True, False, False),
        (Phase.SETTLED, Result.UNKNOWN, None, True, True, True, True, False, False),
        (Phase.SETTLED, None, None, False, True, False, True, False, False),
    ],
)
def test_distinct_operation_truth_tables(
    phase, result, turn, barrier, may_reach, started, cancelled, definitive, accepted
):
    operation = _operation(
        delivery_phase=phase,
        delivery_result=result,
        native_turn_id=turn,
        execution_barrier=barrier,
        error_code="cancelled",
    )
    assert may_have_reached_target(operation) is may_reach
    assert started_task(operation) is started
    assert cancel_delivery_started(operation) is cancelled
    assert cancel_outcome_definitive(operation) is definitive
    assert cancel_accepted(operation) is accepted
    assert accepted_for_play_assignment(operation) is (result is Result.ACCEPTED)
    assert proven_unsent(operation, {"cancelled"}) is (
        phase is Phase.SETTLED and result is Result.REJECTED and turn is None and not barrier
    )


def test_exact_turn_requires_prompt_native_route_and_all_identifiers():
    exact = _operation(
        delivery_phase=Phase.SETTLED,
        backend_generation=1,
        native_session_id="session",
        native_turn_id="turn",
    )
    assert names_exact_native_prompt_turn(exact)
    for change in (
        {"kind": ControlKind.STEER},
        {"transport": ControlTransport.PROVIDER_TERMINAL},
        {"backend_generation": None},
        {"native_session_id": None},
        {"native_turn_id": None},
    ):
        assert not names_exact_native_prompt_turn(replace(exact, **change))
    assert not proven_unsent(replace(exact, delivery_result=Result.REJECTED), {"cancelled"})


def test_sql_started_filter_matches_python_before_limit(store):
    make_participant(store)
    operations = []
    for index, (phase, result, turn) in enumerate(
        product(Phase, (None, Result.ACCEPTED, Result.REJECTED, Result.UNKNOWN), (None, "turn"))
    ):
        handle = f"job-{index}"
        make_job(store, handle=handle, kind="send", created_at=float(index + 1))
        operation = _operation(
            operation_id=f"op-{index}",
            job_handle=handle,
            delivery_phase=phase,
            delivery_result=result,
            native_turn_id=f"turn-{index}" if turn else None,
        )
        store.reserve_control_operation(operation)
        operations.append(operation)
    make_job(store, handle="legacy", kind="send", created_at=100.0)
    make_job(store, handle="spawn", kind="spawn", created_at=101.0)

    expected = ["spawn", "legacy"] + [
        operation.job_handle for operation in reversed(operations) if started_task(operation)
    ]
    for limit in (1, 3, 8, len(expected)):
        assert [job.handle for job in store.recent_turn_jobs("p1", limit=limit)] == expected[:limit]
