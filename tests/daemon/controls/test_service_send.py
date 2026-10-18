"""Control service send: reservation, receipts, deadlines and serialization."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from tests.support.control_harness import (
    Harness,
    RecordingGates,
    make_runtime,
    open_harness,
    state_of,
    wrap_runtime,
)
from tests.support.control_service import native_outcome, operation_rows, terminal_evidence_row
from tests.support.fake_runtime import FakeRuntime
from tests.support.waiting import wait_until
from theater.daemon.controls import ControlService
from theater.daemon.controls import recovery as control_recovery_module
from theater.daemon.controls import service as control_service_module
from theater.daemon.jobs import JobManager
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlReceipt,
    ControlTransport,
    DeliveryResult,
    RuntimeExecutionState,
    SessionOpenMode,
)
from theater.models import (
    BadRequest,
    Busy,
    JobState,
    NotAddressable,
    StaleTarget,
)


async def test_send_reserves_job_and_operation_before_transmission(store: Store) -> None:
    """The DISPATCHED row and the running job exist before the wire is touched."""
    seen: list[tuple[str, bool]] = []

    class ProbeRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            op = store.get_control_operation(operation_id)
            job = None if op is None or op.job_handle is None else store.get_job(op.job_handle)
            seen.append(
                (
                    str(op.delivery_phase) if op else "missing",
                    job is not None and job.state == JobState.RUNNING,
                )
            )
            return await super().send(operation_id=operation_id, prompt=prompt)

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), ProbeRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)

    job = await harness.service.send("p1", caller_id="caller", prompt="do the thing")

    assert seen == [("dispatched", True)]
    assert job.state == JobState.RUNNING
    assert state_of(harness, "p1").sent == ["do the thing"]


async def test_send_receipt_transitions_and_completion(store: Store) -> None:
    """RESERVED -> DISPATCHED -> SETTLED accepted, then exact evidence completes."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")

    job = await harness.service.send("p1", caller_id="caller", prompt="do the thing")
    (op,) = store.control_operations_for_job(job.handle)

    assert op.kind is ControlKind.SEND
    assert op.transport is ControlTransport.NATIVE_RUNTIME
    assert op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert op.delivery_result is DeliveryResult.ACCEPTED
    assert op.native_session_id == state.native_session_id
    assert op.native_turn_id == state.native_turn_id
    assert op.backend_generation == state.backend_generation
    assert job.state == JobState.RUNNING

    finished = await harness.service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=native_outcome(state)
    )
    assert finished is not None
    assert finished.handle == job.handle
    assert finished.state == JobState.DONE
    assert finished.result == "the answer"


async def test_send_runs_the_injected_preflight_and_prompt_gates(store: Store) -> None:
    """Pane/policy preflight and prompt gates run before any reservation."""
    harness = await open_harness(store, "p1")
    recorder = harness.gates_recorder

    await harness.service.send("p1", caller_id="caller", prompt="hello")
    assert recorder.preflights == ["p1"]
    assert recorder.prompt_checks == ["hello"]
    assert recorder.authorized[-1] == ("p1", "caller", "send")

    recorder.refuse_preflight_for.add("p1")
    try:
        await harness.service.send("p1", caller_id="caller", prompt="again")
        raise AssertionError("the preflight gate must refuse the send")
    except StaleTarget:
        pass
    assert state_of(harness, "p1").sent == ["hello"]


async def test_send_rejected_receipt_closes_the_job(store: Store) -> None:
    """A native refusal is a safe failure: settled rejected, job crashed."""
    harness = await open_harness(store, "p1")
    state_of(harness, "p1").reject_code = "context_overfull"

    job = await harness.service.send("p1", caller_id="caller", prompt="doomed")

    assert job.state == JobState.CRASHED
    assert job.error_code == "context_overfull"
    (op,) = store.control_operations_for_job(job.handle)
    assert op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert op.delivery_result is DeliveryResult.REJECTED
    assert op.error_code == "context_overfull"


async def test_send_lost_acknowledgement_is_never_retried(store: Store) -> None:
    """A lost ack after acceptance: op stays DISPATCHED, no resend, no fallback."""

    class ExplodingRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            raise ConnectionError("acknowledgement lost")

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), ExplodingRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")

    job = await harness.service.send("p1", caller_id="caller", prompt="once only")

    assert state.sent == ["once only"]  # the prompt reached the backend exactly once
    assert job.state == JobState.RUNNING  # potentially delivered: never closed early
    (op,) = store.control_operations_for_job(job.handle)
    assert op.delivery_phase is ControlDeliveryPhase.DISPATCHED
    assert op.execution_barrier is True

    # No blind retry: the accepted turn is busy and reconciliation is the only path.
    try:
        await harness.service.send("p1", caller_id="caller", prompt="retry")
        raise AssertionError("a resend must not happen")
    except Busy:
        pass
    assert state.sent == ["once only"]

    # Inside the reconciliation window nothing is forced.
    resolved = await harness.service.reconcile_ambiguous_delivery("p1", now_ts=op.updated_at + 1.0)
    assert resolved == []
    assert store.get_job(job.handle).state == JobState.RUNNING

    # Past the deadline the job finishes crashed with an explicit warning.
    resolved = await harness.service.reconcile_ambiguous_delivery("p1", now_ts=op.updated_at + 31.0)
    assert [j.handle for j in resolved] == [job.handle]
    finished = store.get_job(job.handle)
    assert finished.state == JobState.CRASHED
    assert finished.error_code == "delivery_unknown"
    assert "WARNING" in finished.result
    assert state.sent == ["once only"]  # still no resend, no tmux fallback


async def test_unknown_prompt_deadline_and_barrier_progress_without_manual_dispatch(
    store: Store, monkeypatch
) -> None:
    """Maintenance owns the deadline and later FIFO wakeup, never a retry."""

    class FirstReceiptUnknown(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            if not self.state.sent:
                # The prompt crossed the wire, but the backend could not confirm either a turn or
                # acceptance.
                self.state.sent.append(prompt)
                self.state.native_turn_id = None
                self.state.execution_state = RuntimeExecutionState.UNKNOWN
                return ControlReceipt(
                    operation_id=operation_id,
                    result=DeliveryResult.UNKNOWN,
                    native_turn_id=None,
                )
            return await super().send(operation_id=operation_id, prompt=prompt)

    monkeypatch.setattr(control_service_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(control_recovery_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(control_service_module, "CONTROL_MAINTENANCE_INTERVAL_SECONDS", 0.002)
    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), FirstReceiptUnknown)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    service = harness.service
    state = state_of(harness, "p1")
    service.start(["p1"])
    try:
        first = await service.send("p1", caller_id="caller", prompt="once only")
        second = await service.queue_followup("p1", caller_id="caller", prompt="after idle")

        (first_operation,) = store.control_operations_for_job(first.handle)
        assert first_operation.execution_barrier is True
        assert first_operation.delivery_result is DeliveryResult.UNKNOWN
        assert state.sent == ["once only"]
        assert [operation.job_handle for operation in store.queued_control_operations("p1")] == [
            second.handle
        ]

        # An exact IDLE report for the same generation/session clears only the execution barrier,
        # not the still-running job's delivery deadline.
        state.execution_state = RuntimeExecutionState.IDLE
        await wait_until(lambda: state.sent == ["once only", "after idle"])
        assert store.get_control_operation(first_operation.operation_id).execution_barrier is False
        assert store.get_job(first.handle).state == JobState.RUNNING
        assert store.get_job(second.handle).state == JobState.RUNNING

        await wait_until(lambda: store.get_job(first.handle).state == JobState.CRASHED, timeout=3.0)
        assert store.get_job(first.handle).error_code == "delivery_unknown"
        assert state.sent == ["once only", "after idle"]
    finally:
        await service.aclose()
    assert service.owned_tasks == ()


async def test_cancelled_native_send_keeps_the_daemon_owned_delivery_deadline(
    store: Store, monkeypatch
) -> None:
    """Caller cancellation cannot strand a DISPATCHED native prompt forever."""

    entered = asyncio.Event()

    class BlockingSend(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            self.state.sent.append(prompt)
            self.state.execution_state = RuntimeExecutionState.UNKNOWN
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("the cancelled send must never resume")

    monkeypatch.setattr(control_service_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(control_recovery_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(control_service_module, "CONTROL_MAINTENANCE_INTERVAL_SECONDS", 0.002)
    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), BlockingSend)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    service = harness.service
    service.start(["p1"])
    request = asyncio.create_task(service.send("p1", caller_id="caller", prompt="once only"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        (operation,) = store.dispatched_control_operations("p1")
        assert operation.execution_barrier is True
        assert operation.job_handle is not None
        handle = operation.job_handle

        request.cancel()
        try:
            await request
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the caller task should have been cancelled")

        # The scheduler was armed before the runtime write, so the cancelled
        # request cannot suppress reconciliation, replay, or fallback.
        await wait_until(lambda: store.get_job(handle).state == JobState.CRASHED)
        finished = store.get_job(handle)
        assert finished.error_code == "delivery_unknown"
        assert state_of(harness, "p1").sent == ["once only"]
        assert store.get_control_operation(operation.operation_id).execution_barrier is True
    finally:
        if not request.done():
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request
        await service.aclose()
    assert service.owned_tasks == ()


async def test_unknown_queued_followup_gets_automatic_deadline_without_replay(
    store: Store, monkeypatch
) -> None:
    """The same durable deadline path covers an uncertain FIFO delivery."""

    class QueueReceiptUnknown(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            self.state.sent.append(prompt)
            self.state.native_turn_id = None
            self.state.execution_state = RuntimeExecutionState.UNKNOWN
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.UNKNOWN,
                native_turn_id=None,
            )

    monkeypatch.setattr(control_service_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(control_recovery_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(control_service_module, "CONTROL_MAINTENANCE_INTERVAL_SECONDS", 0.002)
    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), QueueReceiptUnknown)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    service = harness.service
    service.start(["p1"])
    try:
        queued = await service.queue_followup("p1", caller_id="caller", prompt="once only")
        await wait_until(lambda: store.get_job(queued.handle).state == JobState.CRASHED)
        (operation,) = store.control_operations_for_job(queued.handle)
        assert operation.kind is ControlKind.QUEUE_FOLLOWUP
        assert operation.execution_barrier is True
        assert operation.delivery_result is DeliveryResult.UNKNOWN
        assert store.get_job(queued.handle).error_code == "delivery_unknown"
        await asyncio.sleep(0.01)
        assert state_of(harness, "p1").sent == ["once only"]
    finally:
        await service.aclose()


async def test_unknown_prompt_barrier_survives_store_restart(theater_home) -> None:
    """A terminal job timeout is not allowed to erase durable uncertainty."""

    class AckLostRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            raise ConnectionError("lost acknowledgement")

    path = theater_home / "barrier-restart.sqlite"
    first_store = Store(path)
    try:
        harness = Harness(
            first_store,
            {"p1": wrap_runtime(make_runtime("p1"), AckLostRuntime)},
        )
        await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
        job = await harness.service.send("p1", caller_id="caller", prompt="once only")
        (operation,) = first_store.control_operations_for_job(job.handle)
        assert operation.execution_barrier is True
        session = operation.native_session_id
        generation = operation.backend_generation
    finally:
        first_store.close()

    restarted = Store(path)
    try:
        (persisted,) = restarted.execution_barrier_control_operations("p1")
        assert persisted.operation_id == operation.operation_id
        assert persisted.native_session_id == session
        assert persisted.backend_generation == generation
        assert persisted.execution_barrier is True
    finally:
        restarted.close()


async def test_recovery_routes_buffered_evidence_before_an_expired_prompt_deadline(
    store: Store, monkeypatch
) -> None:
    """Recovery does not let an unknown amendment destroy exact prompt evidence."""

    class UnknownPromptAndSteer(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            receipt = await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.UNKNOWN,
                native_turn_id=receipt.native_turn_id,
            )

        async def steer(
            self, *, operation_id: str, native_turn_id: str, prompt: str
        ) -> ControlReceipt:
            await super().steer(
                operation_id=operation_id,
                native_turn_id=native_turn_id,
                prompt=prompt,
            )
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.UNKNOWN,
                native_turn_id=native_turn_id,
            )

    monkeypatch.setattr(control_service_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.001)
    monkeypatch.setattr(control_recovery_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.001)
    monkeypatch.setattr(control_service_module, "CONTROL_MAINTENANCE_INTERVAL_SECONDS", 0.002)
    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), UnknownPromptAndSteer)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service

    original = await service.send("p1", caller_id="caller", prompt="original")
    turn = state.native_turn_id
    await service.steer("p1", caller_id="caller", prompt="uncertain amendment")
    # This models a live source that buffered and durably committed terminal
    # evidence just before a daemon restart, before its old job finish ran.
    assert store.record_native_terminal_evidence(terminal_evidence_row(state, turn)) is True
    state.native_turn_id = None
    state.execution_state = RuntimeExecutionState.UNKNOWN
    await asyncio.sleep(0.01)  # make the pre-restart wall-clock deadline stale

    service.begin_recovery()
    service.start(["p1"])
    try:
        await wait_until(lambda: store.get_job(original.handle).state == JobState.DONE)
        persisted = store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=state.backend_generation,
            native_session_id=state.native_session_id,
            native_turn_id=turn,
        )
        assert persisted is not None
        assert store.get_job(original.handle).result == "the answer"
        (prompt_operation,) = [
            operation
            for operation in store.control_operations_for_job(original.handle)
            if operation.kind is ControlKind.SEND
        ]
        assert prompt_operation.execution_barrier is False
        (steer_operation,) = [
            operation
            for operation in store.control_operations_for_job(original.handle)
            if operation.kind is ControlKind.STEER
        ]
        assert steer_operation.delivery_result is DeliveryResult.UNKNOWN
    finally:
        await service.aclose()


async def test_send_ui_race_records_the_actual_turn(store: Store) -> None:
    """The accepted race: a UI-started turn absorbs the prompt; record that turn."""
    absorb_turn = "turn-human"

    class AbsorbingRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            self.state.sent.append(prompt)
            self.state.native_turn_id = absorb_turn
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.ACCEPTED,
                native_turn_id=absorb_turn,
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), AbsorbingRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")

    job = await harness.service.send("p1", caller_id="caller", prompt="absorbed")

    (op,) = store.control_operations_for_job(job.handle)
    assert op.native_turn_id == absorb_turn  # the actual returned turn, recorded
    assert job.state == JobState.RUNNING
    # The UI turn's terminal evidence finishes our job through the exact mapping.
    finished = await harness.service.record_terminal_evidence(
        "p1",
        backend_generation=op.backend_generation,
        outcome=native_outcome(state, turn=absorb_turn),
    )
    assert finished is not None and finished.handle == job.handle
    assert finished.state == JobState.DONE


async def test_second_job_bound_to_one_turn_fails_closed(store: Store) -> None:
    """Never bind two Theater jobs to one native turn: the newer job closes."""
    shared_turn = "turn-shared"

    class ReplayingRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            self.state.sent.append(prompt)
            self.state.native_turn_id = shared_turn
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.ACCEPTED,
                native_turn_id=shared_turn,
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), ReplayingRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")

    first = await harness.service.send("p1", caller_id="caller", prompt="first")
    (first_op,) = store.control_operations_for_job(first.handle)
    # The shared turn finished; terminal evidence completed the first job.
    await harness.service.record_terminal_evidence(
        "p1",
        backend_generation=first_op.backend_generation,
        outcome=native_outcome(state, turn=shared_turn),
    )
    assert store.get_job(first.handle).state == JobState.DONE
    state.native_turn_id = None
    second = await harness.service.send("p1", caller_id="caller", prompt="second")

    assert store.get_job(first.handle).state == JobState.DONE  # the first binding stands
    assert second.state == JobState.CRASHED
    assert second.error_code == "native_turn_conflict"
    assert "two jobs" in second.result
    # The refused binding never poisons the exact lookup: the turn still
    # maps to exactly the first operation.
    (second_op,) = store.control_operations_for_job(second.handle)
    assert second_op.delivery_result is DeliveryResult.REJECTED
    mapping = store.control_operation_for_native_turn(
        participant_id="p1",
        backend_generation=first_op.backend_generation,
        native_session_id=first_op.native_session_id,
        native_turn_id=shared_turn,
    )
    assert mapping.job_handle == first.handle


async def test_unbound_send_and_queue_fail_without_reserving_or_delivering(store: Store) -> None:
    recorder = RecordingGates()
    service = ControlService(
        store=store,
        jobs=JobManager(store),
        runtime_for=lambda participant_id: None,
        gates=recorder.gates(),
    )

    with pytest.raises(NotAddressable, match="does not offer a transport"):
        await service.send("p1", caller_id="caller", prompt="unbound prompt")
    with pytest.raises(BadRequest, match="does not offer a followup transport"):
        await service.queue_followup("p1", caller_id="caller", prompt="unbound followup")

    assert recorder.delivered == []
    assert operation_rows(store, "p1", ControlKind.SEND) == []
    assert store.queued_control_operations("p1") == []


async def test_second_participant_completes_while_first_blocks(store: Store) -> None:
    """No global lock: B's control completes while A's runtime I/O is blocked."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class GatedRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            receipt = await super().send(operation_id=operation_id, prompt=prompt)
            entered.set()
            await release.wait()
            return receipt

    harness = Harness(
        store,
        {
            "a": wrap_runtime(make_runtime("a"), GatedRuntime),
            "b": make_runtime("b"),
        },
    )
    for runtime in harness.runtimes.values():
        await runtime.open_session(mode=SessionOpenMode.NEW)

    first = asyncio.create_task(harness.service.send("a", caller_id="caller", prompt="slow"))
    await entered.wait()  # A is mid-I/O, holding only A's per-participant lock

    second = await harness.service.send("b", caller_id="caller", prompt="fast")
    assert second.state == JobState.RUNNING
    assert state_of(harness, "b").sent == ["fast"]
    assert not first.done()

    release.set()
    a_job = await first
    assert a_job.state == JobState.RUNNING
    assert state_of(harness, "a").sent == ["slow"]


async def test_maintenance_for_one_participant_does_not_block_another_or_leak_tasks(
    store: Store,
) -> None:
    """A blocked per-participant wakeup never serializes B or daemon teardown."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSnapshot(FakeRuntime):
        async def snapshot(self):
            if getattr(self.state, "block_snapshots", False):
                entered.set()
                await release.wait()
            return await super().snapshot()

    harness = Harness(
        store,
        {
            "a": wrap_runtime(make_runtime("a"), BlockingSnapshot),
            "b": make_runtime("b"),
        },
    )
    for runtime in harness.runtimes.values():
        await runtime.open_session(mode=SessionOpenMode.NEW)
    state_a = state_of(harness, "a")
    state_a.execution_state = RuntimeExecutionState.ACTIVE
    state_a.native_turn_id = None

    # Reservation reads one snapshot, then the scheduled queue wakeup blocks
    # on A's next snapshot while holding only A's lock.
    queued = await harness.service.queue_followup("a", caller_id="caller", prompt="wait")
    state_a.block_snapshots = True
    harness.service.start(["a", "b"])
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        fast = await asyncio.wait_for(
            harness.service.send("b", caller_id="caller", prompt="fast"), timeout=0.5
        )
        assert fast.state == JobState.RUNNING
        assert state_of(harness, "b").sent == ["fast"]
        assert store.get_job(queued.handle).state == JobState.RUNNING
    finally:
        release.set()
        await harness.service.aclose()
    assert harness.service.owned_tasks == ()
