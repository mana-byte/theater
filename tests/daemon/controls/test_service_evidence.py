"""Control service terminal evidence: exact-turn mapping, first-write-wins and reconciliation."""

from __future__ import annotations

import pytest

from tests.support.control_harness import (
    Harness,
    make_operation,
    make_runtime,
    open_harness,
    state_of,
    wrap_runtime,
)
from tests.support.control_service import (
    native_outcome,
    queue_pending,
    terminal_evidence_row,
)
from tests.support.fake_runtime import FakeRuntime
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlReceipt,
    ControlTransport,
    DeliveryResult,
    NativeTurnTerminal,
    SessionOpenMode,
)
from theater.models import (
    JobState,
    now,
)


async def test_crash_between_evidence_and_finish_recovers_exactly_once(store: Store) -> None:
    """Evidence committed, job still running: restart finishes it once."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    jobs = harness.jobs

    job = await service.send("p1", caller_id="caller", prompt="recoverable")
    turn = state.native_turn_id
    # Simulate the crash: the evidence commit landed, the finish did not.
    assert store.record_native_terminal_evidence(terminal_evidence_row(state, turn))
    assert store.get_job(job.handle).state == JobState.RUNNING

    finished = service.finish_jobs_from_pending_evidence(["p1"])

    assert [j.handle for j in finished] == [job.handle]
    done = store.get_job(job.handle)
    assert done.state == JobState.DONE
    assert done.result == "the answer"

    # Exactly once: a second reconciliation pass finds nothing left to finish.
    again = service.finish_jobs_from_pending_evidence(["p1"])
    assert again == []
    assert [f for f in jobs.finishes if f[0] == job.handle] == [(job.handle, "done")]


async def test_terminal_evidence_precedes_job_finish(store: Store) -> None:
    """The evidence row is committed before the job finish becomes visible."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    jobs = harness.jobs

    job = await service.send("p1", caller_id="caller", prompt="witnessed")
    turn = state.native_turn_id
    jobs.evidence_gate[job.handle] = (
        "p1",
        state.backend_generation,
        state.native_session_id,
        turn,
    )

    finished = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=native_outcome(state)
    )
    assert finished is not None and finished.state == JobState.DONE
    # The gate inside SpyJobs.finish asserted the evidence was already there.


async def test_duplicate_and_delayed_evidence_finishes_once(store: Store) -> None:
    """Repeated evidence never finishes twice or rewrites terminal state."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="once only")
    outcome = native_outcome(state)
    first = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=outcome
    )
    second = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=outcome
    )
    late = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=native_outcome(state, result="late rewrite attempt"),
    )

    assert first is not None and first.state == JobState.DONE
    assert second is not None and second.handle == first.handle
    assert late is not None and late.result == "the answer"  # first terminal write wins
    finished = store.get_job(job.handle)
    assert finished.state == JobState.DONE
    assert finished.result == "the answer"
    assert [f for f in harness.jobs.finishes if f[0] == job.handle] == [(job.handle, "done")]


async def test_exact_mapping_only_exact_turn_completes(store: Store) -> None:
    """Wrong session, wrong turn, and no operation complete nothing."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="mine")

    wrong_session = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=native_outcome(state, session="thread-somebody-else"),
    )
    assert wrong_session is None
    assert store.get_job(job.handle).state == JobState.RUNNING

    wrong_turn = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=native_outcome(state, turn="turn-not-mine"),
    )
    assert wrong_turn is None
    assert store.get_job(job.handle).state == JobState.RUNNING

    exact = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=native_outcome(state)
    )
    assert exact is not None and exact.handle == job.handle
    assert exact.state == JobState.DONE


async def test_ambiguous_turn_mapping_fails_closed(store: Store) -> None:
    """Two job-bearing operations on one turn: complete nothing, never guess."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    first = await service.send("p1", caller_id="caller", prompt="first")
    turn = state.native_turn_id
    # Forge the bug state: a second job-bearing operation on the same turn.
    second_handle = "p1#forged"
    harness.jobs.create(
        handle=second_handle,
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="forged twin",
        cwd=None,
    )
    store.reserve_control_operation(
        make_operation(
            f"{second_handle}:send",
            job_handle=second_handle,
            kind=ControlKind.SEND,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.DISPATCHED,
            native_session_id=state.native_session_id,
            native_turn_id=turn,
        )
    )

    finished = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=native_outcome(state)
    )

    assert finished is None  # fails closed: no job is completed on ambiguity
    assert store.get_job(first.handle).state == JobState.RUNNING
    assert store.get_job(second_handle).state == JobState.RUNNING


async def test_reconcile_uses_exact_evidence_and_live_turn(store: Store) -> None:
    """DISPATCHED/UNKNOWN resolve from evidence or a live turn — never by retry."""

    class UncertainRuntime(FakeRuntime):
        """The backend confirms the turn but not the delivery: UNKNOWN."""

        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            receipt = await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.UNKNOWN,
                native_turn_id=receipt.native_turn_id,
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), UncertainRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="uncertain")
    (op,) = store.control_operations_for_job(job.handle)
    assert op.delivery_result is DeliveryResult.UNKNOWN

    # Evidence landed for exactly that turn: reconcile from it, never by retry.
    store.record_native_terminal_evidence(terminal_evidence_row(state, op.native_turn_id))
    resolved = await service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0)
    assert [j.handle for j in resolved] == [job.handle]
    assert store.get_job(job.handle).state == JobState.DONE

    # A turn the authoritative snapshot still shows live is never force-finished,
    # even past the deadline and with no evidence anywhere.
    state.native_turn_id = None  # the finished turn no longer reports busy
    live = await service.send("p1", caller_id="caller", prompt="still running")
    resolved = await service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0)
    assert resolved == []
    assert store.get_job(live.handle).state == JobState.RUNNING


async def test_active_job_selectors_are_exact(store: Store) -> None:
    """Active/queued selectors distinguish delivered work from pending work."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    active = await service.send("p1", caller_id="caller", prompt="active work")
    active_turn = state.native_turn_id
    (queued,) = await queue_pending(harness, ["later work"])

    assert [job.handle for job in service.active_jobs("p1")] == [active.handle]
    assert [job.handle for job in service.queued_jobs("p1")] == [queued.handle]
    found = service.active_job_for_native_turn(
        "p1",
        backend_generation=state.backend_generation,
        native_session_id=state.native_session_id,
        native_turn_id=active_turn,
    )
    assert found is not None and found.handle == active.handle
    assert (
        service.active_job_for_native_turn(
            "p1",
            backend_generation=state.backend_generation,
            native_session_id=state.native_session_id,
            native_turn_id="turn-nobody",
        )
        is None
    )
    # A queued job is never the active job for any turn.
    assert all(job.handle != queued.handle for job in service.active_jobs("p1"))


async def test_duplicate_evidence_across_crash_window_finishes_from_first_write(
    store: Store,
) -> None:
    """Persisted evidence A wins over a conflicting duplicate B after a crash."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="work")
    turn = state.native_turn_id
    # Evidence A commits; the daemon crashes before finishing the job.
    assert store.record_native_terminal_evidence(terminal_evidence_row(state, turn))

    # Conflicting duplicate B arrives while the job still runs.
    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=native_outcome(
            state, turn=turn, terminal=NativeTurnTerminal.FAILED, result="conflicting B"
        ),
    )

    assert finished is not None and finished.handle == job.handle
    assert finished.state == JobState.DONE  # from persisted A, not incoming B
    assert finished.result == "the answer"
    stored = store.get_native_terminal_evidence(
        participant_id="p1",
        backend_generation=state.backend_generation,
        native_session_id=state.native_session_id,
        native_turn_id=turn,
    )
    assert stored.terminal is NativeTurnTerminal.COMPLETED  # the first write stands


async def test_ambiguous_mapping_fails_newer_accepted_job_closed(store: Store) -> None:
    """A newer accepted send never settles into an already-ambiguous turn."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    first = await service.send("p1", caller_id="caller", prompt="first")
    turn = state.native_turn_id
    (first_op,) = store.control_operations_for_job(first.handle)
    # The first turn finished; evidence completed the first job.
    await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=native_outcome(state, turn=turn),
    )
    # Forge the bug state: a second job-bearing operation on the same turn.
    forged_handle = "p1#forged"
    harness.jobs.create(
        handle=forged_handle,
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="forged twin",
        cwd=None,
    )
    store.reserve_control_operation(
        make_operation(
            f"{forged_handle}:send",
            job_handle=forged_handle,
            kind=ControlKind.SEND,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.DISPATCHED,
            native_session_id=first_op.native_session_id,
            native_turn_id=turn,
        )
    )
    harness.jobs.finish(
        forged_handle,
        state=JobState.CRASHED,
        result="forged residue",
        error_code="native_turn_conflict",
    )
    state.native_turn_id = None

    class ReplayingRuntime(FakeRuntime):
        """The backend reports a turn another operation already carries."""

        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.ACCEPTED,
                native_turn_id=turn,
            )

    harness.runtimes["p1"] = wrap_runtime(harness.runtimes["p1"], ReplayingRuntime)

    second = await service.send("p1", caller_id="caller", prompt="second")

    # The newer job fails closed instead of settling into the ambiguity.
    assert second.state == JobState.CRASHED
    assert second.error_code == "native_turn_conflict"
    assert "two jobs" in second.result
    (second_op,) = store.control_operations_for_job(second.handle)
    assert second_op.delivery_result is DeliveryResult.REJECTED
    assert second_op.native_turn_id is None  # never bound into the ambiguity
    # Failing closed completes nothing else: the older jobs are untouched.
    assert store.get_job(first.handle).state == JobState.DONE
    assert store.get_job(forged_handle).state == JobState.CRASHED  # untouched


@pytest.mark.parametrize(
    "relaunch",
    [
        # The backend relaunched: same turn id, different generation.
        pytest.param(
            lambda state: setattr(state, "backend_generation", state.backend_generation + 1),
            id="new-generation",
        ),
        # A new native thread: same turn id, different session.
        pytest.param(
            lambda state: setattr(state, "native_session_id", "thread-reused"), id="new-session"
        ),
    ],
)
async def test_reconcile_turn_id_collision_on_a_new_identity_closes_delivery(
    store: Store, relaunch
) -> None:
    """An id collision after a generation or session change must not postpone the close."""

    class UncertainRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            receipt = await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.UNKNOWN,
                native_turn_id=receipt.native_turn_id,
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), UncertainRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="uncertain")
    relaunch(state)

    resolved = await service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0)

    assert [j.handle for j in resolved] == [job.handle]
    finished = store.get_job(job.handle)
    assert finished.state == JobState.CRASHED
    assert finished.error_code == "delivery_unknown"
