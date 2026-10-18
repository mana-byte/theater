"""Control operations: reservation, dispatch, queue ordering, turn lookup, pruning and bounds."""

from __future__ import annotations

import pytest

from tests.support.runtime_storage import make_job, make_operation
from theater.constants.daemon import CONTROL_OPERATION_PAYLOAD_MAX_BYTES, SEND_SEQ_META_KEY
from theater.constants.harness import HARNESS_RUNTIME_ERROR_MAX_CHARS
from theater.daemon.persistence.repositories.control_operations import (
    ControlOperationAmbiguityError,
)
from theater.daemon.store import Store
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)


def test_operation_reservation_is_idempotent_on_operation_id(store: Store) -> None:
    operation = make_operation(
        "op-1",
        job_handle="job#1",
        provider_id="provider-a",
        provider_generation=4,
        terminal_id="terminal-a",
        terminal_incarnation="incarnation-a",
    )
    store.reserve_control_operation(operation)
    store.reserve_control_operation(operation)
    persisted = store.get_control_operation("op-1")
    assert persisted.job_handle == "job#1"
    assert (
        persisted.provider_id,
        persisted.provider_generation,
        persisted.terminal_id,
        persisted.terminal_incarnation,
    ) == ("provider-a", 4, "terminal-a", "incarnation-a")


def test_operation_dispatch_is_persisted_before_acknowledge(store: Store) -> None:
    store.reserve_control_operation(make_operation("op-1", job_handle="job#1"))
    store.mark_control_operation_dispatched("op-1", native_session_id="thread-1", updated_at=111.0)
    persisted = store.get_control_operation("op-1")
    assert persisted.delivery_phase is ControlDeliveryPhase.DISPATCHED
    assert persisted.delivery_result is None
    assert persisted.native_session_id == "thread-1"
    assert persisted.execution_barrier is True

    store.settle_control_operation(
        "op-1", result=DeliveryResult.UNKNOWN, error_code="delivery_unknown", updated_at=140.0
    )
    persisted = store.get_control_operation("op-1")
    assert persisted.delivery_phase is ControlDeliveryPhase.SETTLED
    assert persisted.delivery_result is DeliveryResult.UNKNOWN
    assert persisted.error_code == "delivery_unknown"
    assert persisted.execution_barrier is True


def test_dispatched_seam_lists_only_actual_dispatch(store: Store) -> None:
    store.reserve_control_operation(make_operation("op-1", job_handle="job#1"))
    store.mark_control_operation_dispatched("op-1", updated_at=111.0)
    store.reserve_control_operation(
        make_operation(
            "op-2",
            job_handle="job#2",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=1,
        )
    )
    dispatched = store.dispatched_control_operations("p1")
    assert [op.operation_id for op in dispatched] == ["op-1"]
    queued = store.queued_control_operations("p1")
    assert [op.operation_id for op in queued] == ["op-2"]


def test_control_operations_in_phases_enumerates_restart_residue(store: Store) -> None:
    """Every RESERVED/DISPATCHED row is enumerated — jobless and job-bearing."""
    # Matches: a jobless reserved settings row, a jobless dispatched interrupt row, and a
    # job-bearing reserved send — the exact hard-crash residue restart must settle.
    store.reserve_control_operation(
        make_operation(
            "op-a",
            kind=ControlKind.SETTINGS_UPDATE,
            transport=ControlTransport.NATIVE_RUNTIME,
        )
    )
    store.reserve_control_operation(
        make_operation(
            "op-b",
            kind=ControlKind.INTERRUPT,
            transport=ControlTransport.NATIVE_RUNTIME,
        )
    )
    store.mark_control_operation_dispatched("op-b", native_session_id="thread-1", updated_at=110.0)
    # Excluded: a jobless settled settings row, a job-bearing queued
    # followup, and another participant's rows.
    store.reserve_control_operation(make_operation("op-job", job_handle="job#1"))
    store.reserve_control_operation(
        make_operation(
            "op-settled",
            kind=ControlKind.SETTINGS_UPDATE,
            transport=ControlTransport.NATIVE_RUNTIME,
        )
    )
    store.settle_control_operation("op-settled", result=DeliveryResult.REJECTED, updated_at=120.0)
    store.reserve_control_operation(
        make_operation(
            "op-queued",
            job_handle="job#2",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=1,
        )
    )
    store.reserve_control_operation(
        make_operation(
            "op-other",
            participant_id="p2",
            kind=ControlKind.INTERRUPT,
            transport=ControlTransport.NATIVE_RUNTIME,
        )
    )
    store.mark_control_operation_dispatched(
        "op-other", native_session_id="thread-2", updated_at=111.0
    )

    phases = (ControlDeliveryPhase.RESERVED, ControlDeliveryPhase.DISPATCHED)
    assert [op.operation_id for op in store.control_operations_in_phases("p1", phases)] == [
        "op-a",
        "op-b",
        "op-job",
    ]
    assert [op.operation_id for op in store.control_operations_in_phases("p2", phases)] == [
        "op-other"
    ]
    assert [op.operation_id for op in store.control_operations_in_phases("p1", ())] == []


def test_queued_followups_order_by_allocated_send_sequence(store: Store) -> None:
    # The persisted send-sequence allocator, never MAX()/timestamps/memory.
    first = store.allocate_control_queue_sequence()
    second = store.allocate_control_queue_sequence()
    third = store.allocate_control_queue_sequence()
    assert (first, second, third) == (1, 2, 3)
    raw = store.get_meta(SEND_SEQ_META_KEY)
    assert raw is not None and int(raw) == 3

    store.reserve_control_operation(
        make_operation(
            "op-3",
            job_handle="job#3",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=third,
        )
    )
    store.reserve_control_operation(
        make_operation(
            "op-1",
            job_handle="job#1",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=first,
        )
    )
    store.reserve_control_operation(
        make_operation(
            "op-2",
            job_handle="job#2",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=second,
        )
    )
    assert [op.operation_id for op in store.queued_control_operations("p1")] == [
        "op-1",
        "op-2",
        "op-3",
    ]
    assert store.queued_control_operation_count("p1") == 3


def test_active_running_jobs_exclude_queued_followups(store: Store) -> None:
    store.create_job(make_job("job#1", created_at=100.0))
    store.create_job(make_job("job#2", created_at=101.0))  # queued followup
    store.create_job(make_job("job#3", created_at=102.0))  # legacy, no operation

    store.reserve_control_operation(make_operation("op-1", job_handle="job#1"))
    store.mark_control_operation_dispatched("op-1", updated_at=110.0)
    store.reserve_control_operation(
        make_operation(
            "op-2",
            job_handle="job#2",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=1,
        )
    )
    # A jobless settings/interrupt operation must not poison the legacy
    # anti-join or make anything active by itself.
    store.reserve_control_operation(make_operation("op-settings", kind=ControlKind.SETTINGS_UPDATE))
    store.mark_control_operation_dispatched("op-settings", updated_at=112.0)
    store.reserve_control_operation(make_operation("op-interrupt", kind=ControlKind.INTERRUPT))
    store.mark_control_operation_dispatched("op-interrupt", updated_at=113.0)

    active = store.active_running_jobs_for_target("p1")
    assert [j.handle for j in active] == ["job#1", "job#3"]

    # The all-running query remains available for cancellation and lifecycle.
    oldest = store.oldest_running_job_for_target("p1")
    assert oldest is not None
    assert oldest.handle == "job#1"


def test_active_running_jobs_track_settled_delivery_results(store: Store) -> None:
    """Accepted and unknown deliveries stay active; rejected do not."""
    store.create_job(make_job("job-accepted", created_at=100.0))
    store.create_job(make_job("job-unknown", created_at=101.0))
    store.create_job(make_job("job-rejected", created_at=102.0))
    store.create_job(make_job("job-reserved", created_at=103.0))

    for operation_id, handle, result in (
        ("op-1", "job-accepted", DeliveryResult.ACCEPTED),
        ("op-2", "job-unknown", DeliveryResult.UNKNOWN),
        ("op-3", "job-rejected", DeliveryResult.REJECTED),
    ):
        store.reserve_control_operation(make_operation(operation_id, job_handle=handle))
        store.mark_control_operation_dispatched(operation_id, updated_at=110.0)
        store.settle_control_operation(operation_id, result=result, updated_at=120.0)
    store.reserve_control_operation(make_operation("op-4", job_handle="job-reserved"))

    active = [job.handle for job in store.active_running_jobs_for_target("p1")]
    # An accepted/unknown delivery keeps the job running until terminal
    # evidence; rejected and merely reserved operations do not.
    assert active == ["job-accepted", "job-unknown"]


def test_active_running_jobs_correlate_operations_by_participant(store: Store) -> None:
    """Another participant's accepted operation cannot activate this job."""
    store.create_job(make_job("job-1", created_at=100.0))
    store.reserve_control_operation(
        make_operation("op-other", participant_id="p2", job_handle="job-1")
    )
    store.mark_control_operation_dispatched("op-other", updated_at=110.0)
    store.settle_control_operation("op-other", result=DeliveryResult.ACCEPTED, updated_at=120.0)

    # No operation belongs to p1, so the job is legacy-active by definition.
    assert [job.handle for job in store.active_running_jobs_for_target("p1")] == ["job-1"]
    assert store.active_running_jobs_for_target("p2") == []


def test_operation_prune_is_bounded_and_settled_only(store: Store) -> None:
    for _ in range(3):
        store.allocate_control_queue_sequence()
    for index in range(4):
        store.reserve_control_operation(
            make_operation(f"op-{index}", job_handle=f"job#{index}", updated_at=100.0 + index)
        )
        store.settle_control_operation(
            f"op-{index}", result=DeliveryResult.REJECTED, updated_at=150.0 + index
        )
    store.reserve_control_operation(
        make_operation(
            "op-queued",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=1,
        )
    )
    removed = store.prune_control_operations(older_than=152.0, limit=2)
    assert removed == 2
    assert store.get_control_operation("op-3") is not None  # newest settled survives
    assert store.get_control_operation("op-0") is None  # oldest settled pruned first
    assert store.get_control_operation("op-queued") is not None  # queued never pruned

    # The allocator counter survives pruned rows.
    assert store.allocate_control_queue_sequence() == 4


def test_operation_prune_retains_rows_tied_to_running_jobs(store: Store) -> None:
    """The SQL itself retains settled rows whose Theater job still runs."""
    store.create_job(make_job("job#1"))
    store.reserve_control_operation(make_operation("op-1", job_handle="job#1"))
    store.mark_control_operation_dispatched("op-1", updated_at=110.0)
    store.settle_control_operation("op-1", result=DeliveryResult.ACCEPTED, updated_at=150.0)

    # Old enough to prune by age, but the mapped job is still running.
    assert store.prune_control_operations(older_than=1000.0) == 0
    assert store.get_control_operation("op-1") is not None

    # Once the job is terminal, the recovery obligation ends.
    store.finish_job("job#1", state="done", result="ok")
    assert store.prune_control_operations(older_than=1000.0) == 1
    assert store.get_control_operation("op-1") is None


def test_operation_prune_releases_jobless_rows_without_running_jobs(store: Store) -> None:
    """Settings/interrupt operations carry no job obligation and prune."""
    store.reserve_control_operation(make_operation("op-settings", kind=ControlKind.SETTINGS_UPDATE))
    store.mark_control_operation_dispatched("op-settings", updated_at=110.0)
    store.settle_control_operation("op-settings", result=DeliveryResult.REJECTED, updated_at=150.0)
    assert store.prune_control_operations(older_than=1000.0) == 1
    assert store.get_control_operation("op-settings") is None


def _turn_lookup(store: Store, **overrides):
    values = {
        "participant_id": "p1",
        "backend_generation": 1,
        "native_session_id": "thread-1",
        "native_turn_id": "turn-1",
    }
    values.update(overrides)
    return store.control_operation_for_native_turn(**values)


def test_control_operation_lookup_by_exact_native_turn(store: Store) -> None:
    store.create_job(make_job("job#1"))
    store.reserve_control_operation(
        make_operation("op-1", job_handle="job#1", backend_generation=1)
    )
    store.mark_control_operation_dispatched(
        "op-1", native_session_id="thread-1", native_turn_id="turn-1", updated_at=110.0
    )
    found = _turn_lookup(store)
    assert found is not None
    assert found.operation_id == "op-1"

    # SETTLED with ACCEPTED delivery still maps the running job.
    store.settle_control_operation(
        "op-1", result=DeliveryResult.ACCEPTED, native_turn_id="turn-1", updated_at=120.0
    )
    found = _turn_lookup(store)
    assert found is not None
    assert found.operation_id == "op-1"

    # Wrong generation, session, or participant never match, and the lookup
    # never falls back to an unrelated oldest-running job.
    assert _turn_lookup(store, backend_generation=2) is None
    assert _turn_lookup(store, native_session_id="thread-9") is None
    assert _turn_lookup(store, participant_id="p2") is None
    assert _turn_lookup(store, native_turn_id="turn-9") is None


def test_control_operation_lookup_excludes_undelivered_or_rejected(store: Store) -> None:
    store.create_job(make_job("job-queued"))
    store.reserve_control_operation(
        make_operation(
            "op-queued",
            job_handle="job-queued",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=1,
            backend_generation=1,
        )
    )
    store.create_job(make_job("job-reserved"))
    store.reserve_control_operation(make_operation("op-reserved", job_handle="job-reserved"))
    store.create_job(make_job("job-rejected"))
    store.reserve_control_operation(
        make_operation("op-rejected", job_handle="job-rejected", backend_generation=1)
    )
    store.mark_control_operation_dispatched(
        "op-rejected", native_session_id="thread-1", native_turn_id="turn-1", updated_at=110.0
    )
    store.settle_control_operation(
        "op-rejected", result=DeliveryResult.REJECTED, native_turn_id="turn-1", updated_at=120.0
    )
    # A jobless settled-accepted operation maps no Theater job either.
    store.reserve_control_operation(
        make_operation("op-settings", kind=ControlKind.SETTINGS_UPDATE, backend_generation=1)
    )
    store.mark_control_operation_dispatched(
        "op-settings", native_session_id="thread-1", native_turn_id="turn-1", updated_at=111.0
    )
    store.settle_control_operation(
        "op-settings", result=DeliveryResult.ACCEPTED, native_turn_id="turn-1", updated_at=121.0
    )

    assert _turn_lookup(store) is None


def test_control_operation_lookup_ambiguity_fails_closed(store: Store) -> None:
    """Two job-bearing operations on one exact turn are a bug state."""
    store.create_job(make_job("job#1"))
    store.create_job(make_job("job#2"))
    for operation_id, handle in (("op-1", "job#1"), ("op-2", "job#2")):
        store.reserve_control_operation(
            make_operation(operation_id, job_handle=handle, backend_generation=1)
        )
        store.mark_control_operation_dispatched(
            operation_id, native_session_id="thread-1", native_turn_id="turn-1", updated_at=110.0
        )
    with pytest.raises(ControlOperationAmbiguityError):
        _turn_lookup(store)


def test_control_operation_lookup_ignores_steer_rows_sharing_the_turn(store: Store) -> None:
    """STEER legitimately shares its turn's identity and never poisons the lookup."""
    store.create_job(make_job("job#1"))
    store.reserve_control_operation(
        make_operation("op-send", job_handle="job#1", backend_generation=1)
    )
    store.mark_control_operation_dispatched(
        "op-send", native_session_id="thread-1", native_turn_id="turn-1", updated_at=110.0
    )
    store.reserve_control_operation(
        make_operation("op-steer", kind=ControlKind.STEER, backend_generation=1)
    )
    store.mark_control_operation_dispatched(
        "op-steer", native_session_id="thread-1", native_turn_id="turn-1", updated_at=111.0
    )
    found = _turn_lookup(store)
    assert found is not None
    assert found.operation_id == "op-send"


def test_allocate_control_queue_sequence_is_transaction_aware(store: Store) -> None:
    """The allocator reads and writes through the caller's transaction."""
    with store.runtime_transaction() as conn:
        assert store.get_control_queue_sequence(connection=conn) == 0
        assert store.allocate_control_queue_sequence(connection=conn) == 1
    assert store.get_control_queue_sequence() == 1
    assert store.allocate_control_queue_sequence() == 2
    assert store.get_control_queue_sequence() == 2


def test_allocate_control_queue_sequence_rollback_is_not_persisted(store: Store) -> None:
    with (
        pytest.raises(RuntimeError, match="queue reservation failed"),
        store.runtime_transaction() as conn,
    ):
        store.allocate_control_queue_sequence(connection=conn)
        raise RuntimeError("queue reservation failed")
    # The rolled-back increment never became visible.
    assert store.get_control_queue_sequence() == 0


def test_control_operation_rejects_malformed_rows(store: Store) -> None:
    with pytest.raises(ValueError, match="operation_id"):
        store.reserve_control_operation(make_operation(" "))
    with pytest.raises(ValueError, match="participant_id"):
        store.reserve_control_operation(make_operation("op-1", participant_id=""))
    with pytest.raises(TypeError, match="kind"):
        store.reserve_control_operation(make_operation("op-1", kind="send"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="transport"):
        store.reserve_control_operation(
            make_operation("op-1", transport="native")  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="backend_generation"):
        store.reserve_control_operation(make_operation("op-1", backend_generation=-1))
    with pytest.raises(ValueError, match="queue_sequence"):
        store.reserve_control_operation(make_operation("op-1", queue_sequence=-1))
    with pytest.raises(ValueError, match="created_at"):
        store.reserve_control_operation(make_operation("op-1", created_at=-1.0))
    with pytest.raises(ValueError, match="updated_at"):
        store.reserve_control_operation(make_operation("op-1", updated_at=True))
    with pytest.raises(ValueError, match="error"):
        store.reserve_control_operation(
            make_operation("op-1", error="x" * (HARNESS_RUNTIME_ERROR_MAX_CHARS + 1))
        )
    assert store.get_control_operation("op-1") is None


def test_operation_payload_is_bounded_in_utf8_bytes(store: Store) -> None:
    # Exactly at the bound is accepted; UTF-8 bytes count, not characters.
    at_bound = "x" * CONTROL_OPERATION_PAYLOAD_MAX_BYTES
    store.reserve_control_operation(make_operation("op-ok", payload=at_bound))
    assert store.get_control_operation("op-ok").payload == at_bound

    with pytest.raises(ValueError, match="UTF-8 bytes"):
        store.reserve_control_operation(make_operation("op-overflow", payload=at_bound + "x"))

    # Multibyte characters: the character count fits, the byte count does not.
    multibyte = "é" * (CONTROL_OPERATION_PAYLOAD_MAX_BYTES - 1)
    assert len(multibyte) <= CONTROL_OPERATION_PAYLOAD_MAX_BYTES
    assert len(multibyte.encode("utf-8")) > CONTROL_OPERATION_PAYLOAD_MAX_BYTES
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        store.reserve_control_operation(make_operation("op-multibyte", payload=multibyte))
    assert store.get_control_operation("op-multibyte") is None
