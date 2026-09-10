"""Runtime storage: bindings, control operations, terminal evidence, migration.

Covers the three daemon-owned storage concepts frozen in the runtime-contracts
wave: participant runtime bindings, control operations, and native terminal
evidence — including the transaction boundaries (persist launch intent before
backend start; persist exact identity before initial dispatch; persist
terminal evidence before exposing completion), queue ordering via the
persisted send-sequence allocator, the dispatched/active-job seams, and
bounded pruning.
"""

from __future__ import annotations

import json

import pytest

from theater.constants.daemon import CONTROL_OPERATION_PAYLOAD_MAX_BYTES, SEND_SEQ_META_KEY
from theater.constants.harness import (
    HARNESS_RUNTIME_ERROR_MAX_CHARS,
    HARNESS_RUNTIME_POLICY_MAX_CHARS,
    HARNESS_RUNTIME_RESULT_MAX_CHARS,
)
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
    encode_launch_policy,
)
from theater.daemon.store import Store
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import Job, JobState


def _binding(participant_id: str = "p1", **overrides) -> ParticipantRuntimeBinding:
    values = {
        "participant_id": participant_id,
        "harness": "codex",
        "wiring": RuntimeWiring.NATIVE,
        "backend_generation": 1,
        "lifecycle": RuntimeLifecyclePhase.INTENDED,
        "endpoint": "unix:///tmp/private.sock",
        "launch_policy": encode_launch_policy({"approval": "on-request", "model": "m1"}),
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    values.update(overrides)
    return ParticipantRuntimeBinding(**values)


def _operation(operation_id: str, participant_id: str = "p1", **overrides) -> ControlOperation:
    values = {
        "operation_id": operation_id,
        "participant_id": participant_id,
        "kind": ControlKind.SEND,
        "transport": ControlTransport.NATIVE_RUNTIME,
        "delivery_phase": ControlDeliveryPhase.RESERVED,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    values.update(overrides)
    return ControlOperation(**values)


def _evidence(
    participant_id: str = "p1",
    native_turn_id: str = "turn-1",
    **overrides,
) -> NativeTerminalEvidence:
    values = {
        "participant_id": participant_id,
        "backend_generation": 1,
        "native_session_id": "thread-1",
        "native_turn_id": native_turn_id,
        "terminal": NativeTurnTerminal.COMPLETED,
        "result": "the answer",
        "completeness": ResultCompleteness.COMPLETE,
        "provenance": ResultProvenance.NATIVE_EVIDENCE,
        "recorded_at": 200.0,
    }
    values.update(overrides)
    return NativeTerminalEvidence(**values)


# ---- migration -----------------------------------------------------------


def test_migration_created_runtime_tables(store: Store) -> None:
    tables = set(
        store.conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars()
    )
    assert {
        "participant_runtime_bindings",
        "control_operations",
        "native_terminal_evidence",
    } <= tables

    binding_cols = {
        row[1]
        for row in store.conn.exec_driver_sql(
            "PRAGMA table_info(participant_runtime_bindings)"
        ).fetchall()
    }
    assert {
        "participant_id",
        "harness",
        "wiring",
        "backend_generation",
        "lifecycle_phase",
        "endpoint",
        "backend_pid",
        "backend_started_at",
        "native_session_id",
        "protocol",
        "protocol_version",
        "native_version",
        "compatibility_policy",
        "launch_policy",
    } <= binding_cols

    op_cols = {
        row[1]
        for row in store.conn.exec_driver_sql("PRAGMA table_info(control_operations)").fetchall()
    }
    assert {
        "operation_id",
        "participant_id",
        "job_handle",
        "kind",
        "transport",
        "delivery_phase",
        "delivery_result",
        "backend_generation",
        "native_session_id",
        "native_turn_id",
        "queue_sequence",
        "payload",
    } <= op_cols

    evidence_cols = {
        row[1]
        for row in store.conn.exec_driver_sql(
            "PRAGMA table_info(native_terminal_evidence)"
        ).fetchall()
    }
    assert {
        "participant_id",
        "backend_generation",
        "native_session_id",
        "native_turn_id",
        "terminal",
        "result",
        "result_completeness",
        "result_provenance",
        "error_code",
        "error",
        "recorded_at",
    } == evidence_cols


# ---- participant runtime bindings ----------------------------------------


def test_launch_intent_persists_before_backend_start(store: Store) -> None:
    binding = _binding()
    with store.runtime_transaction() as conn:
        store.upsert_runtime_binding(binding, connection=conn)
        # The participant reservation would share this transaction.
    persisted = store.get_runtime_binding("p1")
    assert persisted is not None
    assert persisted.lifecycle is RuntimeLifecyclePhase.INTENDED
    assert persisted.native_session_id is None
    assert persisted.backend_pid is None
    assert json.loads(persisted.launch_policy or "{}") == {"approval": "on-request", "model": "m1"}


def test_launch_intent_rolls_back_when_the_transaction_fails(store: Store) -> None:
    with (
        pytest.raises(RuntimeError, match="spawn reservation failed"),
        store.runtime_transaction() as conn,
    ):
        store.upsert_runtime_binding(_binding(), connection=conn)
        raise RuntimeError("spawn reservation failed")
    # The rolled-back intent is invisible outside the transaction.
    assert store.get_runtime_binding("p1") is None


def test_identity_persists_before_initial_dispatch(store: Store) -> None:
    store.upsert_runtime_binding(_binding())
    assert store.mark_runtime_backend_started(
        "p1", backend_generation=1, pid=4242, started_at=110.0
    )
    persisted = store.get_runtime_binding("p1")
    assert persisted.lifecycle is RuntimeLifecyclePhase.STARTED
    assert persisted.backend_pid == 4242

    with store.runtime_transaction() as conn:
        assert store.bind_runtime_identity(
            "p1",
            backend_generation=1,
            native_session_id="thread-1",
            protocol="websocket-jsonrpc",
            protocol_version="0.154.0",
            native_version="0.154.0",
            compatibility_policy="codex-0.154-verified",
            updated_at=120.0,
            connection=conn,
        )
    persisted = store.get_runtime_binding("p1")
    assert persisted.lifecycle is RuntimeLifecyclePhase.BOUND
    assert persisted.native_session_id == "thread-1"


def test_recoverable_bindings_exclude_terminal_phases(store: Store) -> None:
    store.upsert_runtime_binding(_binding("p1"))
    store.upsert_runtime_binding(_binding("p2", lifecycle=RuntimeLifecyclePhase.DETACHED))
    store.upsert_runtime_binding(_binding("p3", lifecycle=RuntimeLifecyclePhase.STOPPED))
    store.upsert_runtime_binding(
        _binding("p4", wiring=RuntimeWiring.LEGACY, lifecycle=RuntimeLifecyclePhase.FAILED)
    )
    recoverable = {binding.participant_id for binding in store.runtime_bindings_for_recovery()}
    assert recoverable == {"p1", "p2"}


def test_binding_lookup_by_exact_native_session(store: Store) -> None:
    store.upsert_runtime_binding(_binding())
    store.bind_runtime_identity(
        "p1",
        backend_generation=1,
        native_session_id="thread-1",
        updated_at=120.0,
    )
    assert store.runtime_binding_by_native_session("thread-1") is not None
    assert store.runtime_binding_by_native_session("thread-2") is None


def test_binding_lifecycle_transitions_and_delete(store: Store) -> None:
    store.upsert_runtime_binding(_binding())
    assert store.set_runtime_lifecycle(
        "p1", RuntimeLifecyclePhase.ACTIVE, backend_generation=1, updated_at=130.0
    )
    assert store.get_runtime_binding("p1").lifecycle is RuntimeLifecyclePhase.ACTIVE
    store.delete_runtime_binding("p1")
    assert store.get_runtime_binding("p1") is None


def test_stale_generation_cannot_mutate_the_current_generation(store: Store) -> None:
    """A delayed generation-1 callback must never touch generation 2's row."""
    store.upsert_runtime_binding(_binding())
    store.upsert_runtime_binding(
        _binding(
            backend_generation=2,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            endpoint="unix:///tmp/private-2.sock",
        )
    )
    store.mark_runtime_backend_started("p1", backend_generation=2, pid=9999, started_at=200.0)
    store.bind_runtime_identity(
        "p1", backend_generation=2, native_session_id="thread-2", updated_at=210.0
    )

    # Every stale generation-1 mutation fails explicitly and changes nothing.
    assert (
        store.mark_runtime_backend_started("p1", backend_generation=1, pid=1111, started_at=220.0)
        is False
    )
    assert (
        store.bind_runtime_identity(
            "p1", backend_generation=1, native_session_id="thread-stale", updated_at=230.0
        )
        is False
    )
    assert (
        store.set_runtime_lifecycle(
            "p1", RuntimeLifecyclePhase.ATTACHED, backend_generation=1, updated_at=240.0
        )
        is False
    )

    persisted = store.get_runtime_binding("p1")
    assert persisted.backend_generation == 2
    assert persisted.backend_pid == 9999
    assert persisted.native_session_id == "thread-2"
    assert persisted.lifecycle is RuntimeLifecyclePhase.BOUND


# ---- control operations ----------------------------------------------------


def test_operation_reservation_is_idempotent_on_operation_id(store: Store) -> None:
    operation = _operation("op-1", job_handle="job#1")
    store.reserve_control_operation(operation)
    store.reserve_control_operation(operation)
    assert store.get_control_operation("op-1").job_handle == "job#1"


def test_operation_dispatch_is_persisted_before_acknowledge(store: Store) -> None:
    store.reserve_control_operation(_operation("op-1", job_handle="job#1"))
    store.mark_control_operation_dispatched("op-1", native_session_id="thread-1", updated_at=111.0)
    persisted = store.get_control_operation("op-1")
    assert persisted.delivery_phase is ControlDeliveryPhase.DISPATCHED
    assert persisted.delivery_result is None
    assert persisted.native_session_id == "thread-1"

    store.settle_control_operation(
        "op-1", result=DeliveryResult.UNKNOWN, error_code="delivery_unknown", updated_at=140.0
    )
    persisted = store.get_control_operation("op-1")
    assert persisted.delivery_phase is ControlDeliveryPhase.SETTLED
    assert persisted.delivery_result is DeliveryResult.UNKNOWN
    assert persisted.error_code == "delivery_unknown"


def test_dispatched_seam_lists_only_actual_dispatch(store: Store) -> None:
    store.reserve_control_operation(_operation("op-1", job_handle="job#1"))
    store.mark_control_operation_dispatched("op-1", updated_at=111.0)
    store.reserve_control_operation(
        _operation(
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


def test_queued_followups_order_by_allocated_send_sequence(store: Store) -> None:
    # The persisted send-sequence allocator, never MAX()/timestamps/memory.
    first = store.allocate_control_queue_sequence()
    second = store.allocate_control_queue_sequence()
    third = store.allocate_control_queue_sequence()
    assert (first, second, third) == (1, 2, 3)
    raw = store.get_meta(SEND_SEQ_META_KEY)
    assert raw is not None and int(raw) == 3

    store.reserve_control_operation(
        _operation(
            "op-3",
            job_handle="job#3",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=third,
        )
    )
    store.reserve_control_operation(
        _operation(
            "op-1",
            job_handle="job#1",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=first,
        )
    )
    store.reserve_control_operation(
        _operation(
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


def _job(handle: str, created_at: float = 100.0, target_id: str = "p1") -> Job:
    return Job(
        handle=handle,
        caller_id="caller",
        target_id=target_id,
        kind="send",
        prompt=None,
        state=JobState.RUNNING,
        result=None,
        error_code=None,
        created_at=created_at,
        finished_at=None,
    )


def test_active_running_jobs_exclude_queued_followups(store: Store) -> None:
    store.create_job(_job("job#1", created_at=100.0))
    store.create_job(_job("job#2", created_at=101.0))  # queued followup
    store.create_job(_job("job#3", created_at=102.0))  # legacy, no operation

    store.reserve_control_operation(_operation("op-1", job_handle="job#1"))
    store.mark_control_operation_dispatched("op-1", updated_at=110.0)
    store.reserve_control_operation(
        _operation(
            "op-2",
            job_handle="job#2",
            kind=ControlKind.QUEUE_FOLLOWUP,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            queue_sequence=1,
        )
    )
    # A jobless settings/interrupt operation must not poison the legacy
    # anti-join or make anything active by itself.
    store.reserve_control_operation(_operation("op-settings", kind=ControlKind.SETTINGS_UPDATE))
    store.mark_control_operation_dispatched("op-settings", updated_at=112.0)
    store.reserve_control_operation(_operation("op-interrupt", kind=ControlKind.INTERRUPT))
    store.mark_control_operation_dispatched("op-interrupt", updated_at=113.0)

    active = store.active_running_jobs_for_target("p1")
    assert [j.handle for j in active] == ["job#1", "job#3"]

    # The all-running query remains available for cancellation and lifecycle.
    oldest = store.oldest_running_job_for_target("p1")
    assert oldest is not None
    assert oldest.handle == "job#1"


def test_active_running_jobs_track_settled_delivery_results(store: Store) -> None:
    """Accepted and unknown deliveries stay active; rejected do not."""
    store.create_job(_job("job-accepted", created_at=100.0))
    store.create_job(_job("job-unknown", created_at=101.0))
    store.create_job(_job("job-rejected", created_at=102.0))
    store.create_job(_job("job-reserved", created_at=103.0))

    for operation_id, handle, result in (
        ("op-1", "job-accepted", DeliveryResult.ACCEPTED),
        ("op-2", "job-unknown", DeliveryResult.UNKNOWN),
        ("op-3", "job-rejected", DeliveryResult.REJECTED),
    ):
        store.reserve_control_operation(_operation(operation_id, job_handle=handle))
        store.mark_control_operation_dispatched(operation_id, updated_at=110.0)
        store.settle_control_operation(operation_id, result=result, updated_at=120.0)
    store.reserve_control_operation(_operation("op-4", job_handle="job-reserved"))

    active = [job.handle for job in store.active_running_jobs_for_target("p1")]
    # An accepted/unknown delivery keeps the job running until terminal
    # evidence; rejected and merely reserved operations do not.
    assert active == ["job-accepted", "job-unknown"]


def test_active_running_jobs_correlate_operations_by_participant(store: Store) -> None:
    """Another participant's accepted operation cannot activate this job."""
    store.create_job(_job("job-1", created_at=100.0))
    store.reserve_control_operation(_operation("op-other", participant_id="p2", job_handle="job-1"))
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
            _operation(f"op-{index}", job_handle=f"job#{index}", updated_at=100.0 + index)
        )
        store.settle_control_operation(
            f"op-{index}", result=DeliveryResult.REJECTED, updated_at=150.0 + index
        )
    store.reserve_control_operation(
        _operation(
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


# ---- native terminal evidence ------------------------------------------------


def test_evidence_first_write_wins(store: Store) -> None:
    evidence = _evidence()
    assert store.record_native_terminal_evidence(evidence) is True
    replay = _evidence(result="late duplicate")
    assert store.record_native_terminal_evidence(replay) is False

    persisted = store.get_native_terminal_evidence(
        participant_id="p1",
        backend_generation=1,
        native_session_id="thread-1",
        native_turn_id="turn-1",
    )
    assert persisted is not None
    assert persisted.terminal is NativeTurnTerminal.COMPLETED
    assert persisted.result == "the answer"
    assert persisted.completeness is ResultCompleteness.COMPLETE
    assert persisted.provenance is ResultProvenance.NATIVE_EVIDENCE


def test_evidence_records_before_completion_is_exposed(store: Store) -> None:
    store.upsert_runtime_binding(_binding())
    store.bind_runtime_identity(
        "p1", backend_generation=1, native_session_id="thread-1", updated_at=120.0
    )
    job = Job(
        handle="job#1",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt=None,
        state=JobState.RUNNING,
        result=None,
        error_code=None,
        created_at=100.0,
        finished_at=None,
    )
    store.create_job(job)

    # The frozen boundary: terminal evidence is persisted before completion is
    # exposed. A crash between the two commits leaves recoverable evidence and
    # a still-running job; reconciliation finishes the job without replaying
    # the prompt.
    assert store.record_native_terminal_evidence(_evidence()) is True
    store.finish_job("job#1", state="done", result="the answer")

    assert store.get_job("job#1").state == JobState.DONE.value
    assert (
        store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=1,
            native_session_id="thread-1",
            native_turn_id="turn-1",
        )
        is not None
    )


def test_evidence_recovery_after_crash_before_completion(store: Store) -> None:
    # Crash happened between recording evidence and finishing the job.
    assert store.record_native_terminal_evidence(_evidence()) is True
    job = Job(
        handle="job#1",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt=None,
        state=JobState.RUNNING,
        result=None,
        error_code=None,
        created_at=100.0,
        finished_at=None,
    )
    store.create_job(job)
    recovered = store.native_terminal_evidence_for_participant("p1")
    assert [e.native_turn_id for e in recovered] == ["turn-1"]


def test_evidence_prune_is_bounded(store: Store) -> None:
    for index in range(3):
        assert store.record_native_terminal_evidence(
            _evidence(native_turn_id=f"turn-{index}", recorded_at=200.0 + index)
        )
    removed = store.prune_native_terminal_evidence(older_than=202.0, limit=1)
    assert removed == 1
    assert (
        store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=1,
            native_session_id="thread-1",
            native_turn_id="turn-2",
        )
        is not None
    )
    assert (
        store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=1,
            native_session_id="thread-1",
            native_turn_id="turn-0",
        )
        is None
    )


# ---- declared bounds -------------------------------------------------------


def test_operation_payload_is_bounded_in_utf8_bytes(store: Store) -> None:
    # Exactly at the bound is accepted; UTF-8 bytes count, not characters.
    at_bound = "x" * CONTROL_OPERATION_PAYLOAD_MAX_BYTES
    store.reserve_control_operation(_operation("op-ok", payload=at_bound))
    assert store.get_control_operation("op-ok").payload == at_bound

    with pytest.raises(ValueError, match="UTF-8 bytes"):
        store.reserve_control_operation(_operation("op-overflow", payload=at_bound + "x"))

    # Multibyte characters: the character count fits, the byte count does not.
    multibyte = "é" * (CONTROL_OPERATION_PAYLOAD_MAX_BYTES - 1)
    assert len(multibyte) <= CONTROL_OPERATION_PAYLOAD_MAX_BYTES
    assert len(multibyte.encode("utf-8")) > CONTROL_OPERATION_PAYLOAD_MAX_BYTES
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        store.reserve_control_operation(_operation("op-multibyte", payload=multibyte))
    assert store.get_control_operation("op-multibyte") is None


def test_binding_rejects_malformed_or_oversized_launch_policy(store: Store) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        store.upsert_runtime_binding(_binding(launch_policy="not json"))
    with pytest.raises(ValueError, match="JSON object"):
        store.upsert_runtime_binding(_binding(launch_policy=json.dumps(["not", "an", "object"])))
    # NaN decodes as a float in Python's JSON, but is not a finite JSON number.
    with pytest.raises(ValueError, match="finite"):
        store.upsert_runtime_binding(_binding(launch_policy='{"loss": NaN}'))
    with pytest.raises(ValueError, match="compatibility_policy"):
        store.upsert_runtime_binding(
            _binding(compatibility_policy="x" * (HARNESS_RUNTIME_POLICY_MAX_CHARS + 1))
        )
    assert store.get_runtime_binding("p1") is None


def test_evidence_rejects_values_beyond_public_contract_bounds(store: Store) -> None:
    oversized_result = "x" * (HARNESS_RUNTIME_RESULT_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="result"):
        store.record_native_terminal_evidence(_evidence(result=oversized_result))
    with pytest.raises(ValueError, match="error"):
        store.record_native_terminal_evidence(
            _evidence(error="x" * (HARNESS_RUNTIME_ERROR_MAX_CHARS + 1))
        )
    with pytest.raises(ValueError, match="native_session_id"):
        store.record_native_terminal_evidence(_evidence(native_session_id=""))
    # A result at the bound persists exactly, never truncated.
    at_bound = "x" * HARNESS_RUNTIME_RESULT_MAX_CHARS
    assert store.record_native_terminal_evidence(_evidence(result=at_bound))
    persisted = store.get_native_terminal_evidence(
        participant_id="p1",
        backend_generation=1,
        native_session_id="thread-1",
        native_turn_id="turn-1",
    )
    assert persisted.result == at_bound
