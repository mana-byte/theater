"""Native terminal evidence: first-write-wins, crash recovery, pruning and bounds."""

from __future__ import annotations

import pytest

from tests.support.runtime_storage import make_binding, make_job, make_operation
from theater.constants.harness import (
    HARNESS_RUNTIME_ERROR_MAX_CHARS,
    HARNESS_RUNTIME_RESULT_MAX_CHARS,
)
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.store import Store
from theater.harness.contracts.runtime import (
    DeliveryResult,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
)
from theater.models import Job, JobState


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


def test_evidence_first_write_wins(store: Store) -> None:
    evidence = _evidence(from_history=True, completed_at=99.0)
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
    assert persisted.from_history is True
    assert persisted.completed_at == 99.0


def test_evidence_records_before_completion_is_exposed(store: Store) -> None:
    store.upsert_runtime_binding(make_binding())
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

    # The frozen boundary: terminal evidence is persisted before completion is exposed.
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


def test_evidence_prune_retains_evidence_for_running_jobs(store: Store) -> None:
    """Evidence mapped to a running job survives; obligation ends with the job."""
    store.create_job(make_job("job#1"))
    store.reserve_control_operation(
        make_operation("op-1", job_handle="job#1", backend_generation=1)
    )
    store.mark_control_operation_dispatched(
        "op-1", native_session_id="thread-1", native_turn_id="turn-1", updated_at=110.0
    )
    store.settle_control_operation(
        "op-1", result=DeliveryResult.ACCEPTED, native_turn_id="turn-1", updated_at=120.0
    )
    assert store.record_native_terminal_evidence(_evidence(recorded_at=150.0))

    # Old enough to prune, but the exact matching operation still maps a
    # running job whose recovery may need this evidence.
    assert store.prune_native_terminal_evidence(older_than=1000.0) == 0
    assert (
        store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=1,
            native_session_id="thread-1",
            native_turn_id="turn-1",
        )
        is not None
    )

    store.finish_job("job#1", state="done", result="the answer")
    assert store.prune_native_terminal_evidence(older_than=1000.0) == 1
    assert (
        store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=1,
            native_session_id="thread-1",
            native_turn_id="turn-1",
        )
        is None
    )


def test_evidence_prune_without_matching_operation_stays_bounded(store: Store) -> None:
    """Evidence no operation maps to a running job prunes by age as before."""
    assert store.record_native_terminal_evidence(_evidence(native_turn_id="turn-a"))
    assert store.record_native_terminal_evidence(
        _evidence(native_turn_id="turn-b", recorded_at=201.0)
    )
    assert store.prune_native_terminal_evidence(older_than=1000.0) == 2


def test_evidence_and_binding_reject_malformed_rows(store: Store) -> None:
    with pytest.raises(ValueError, match="participant_id"):
        store.record_native_terminal_evidence(_evidence(participant_id=""))
    with pytest.raises(ValueError, match="backend_generation"):
        store.record_native_terminal_evidence(_evidence(backend_generation=-1))
    with pytest.raises(ValueError, match="recorded_at"):
        store.record_native_terminal_evidence(_evidence(recorded_at=-1.0))
    assert store.native_terminal_evidence_for_participant("p1") == []

    with pytest.raises(ValueError, match="harness"):
        store.upsert_runtime_binding(make_binding(harness=""))
    with pytest.raises(ValueError, match="backend_generation"):
        store.upsert_runtime_binding(make_binding(backend_generation=-1))
    with pytest.raises(ValueError, match="created_at"):
        store.upsert_runtime_binding(make_binding(created_at=-1.0))
    with pytest.raises(ValueError, match="updated_at"):
        store.upsert_runtime_binding(make_binding(updated_at=True))
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
