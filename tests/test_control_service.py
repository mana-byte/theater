"""The daemon control service: durable control state machine over fake runtimes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.daemon.controls import ControlGates, ControlService
from theater.daemon.controls import service as control_service_module
from theater.daemon.jobs import JobManager
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.persistence.store import Store
from theater.daemon.schema import control_operations as control_operations_table
from theater.daemon.schema import touch as touch_table
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST as OPENCODE_MANIFEST
from theater.harness.contracts.events import EventPath
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ControlDeliveryPhase,
    ControlKind,
    ControlReceipt,
    ControlTransport,
    DeliveryResult,
    NativeHumanInteraction,
    NativeInteractionKind,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeCapability,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeLifecyclePhase,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.models import (
    AwaitingDecision,
    BadRequest,
    Busy,
    HumanPresent,
    Job,
    JobState,
    NotYourChild,
    StaleTarget,
    now,
)

PENDING_INTERACTION = NativeHumanInteraction(kind=NativeInteractionKind.APPROVAL)
BUSY_TURN = "turn-keeps-queue-pending"


# ---- harness ------------------------------------------------------------


def make_runtime(participant_id: str, *, generation: int = 1) -> FakeRuntime:
    """A fake runtime sharing its state with the injected fake I/O."""
    state = FakeRuntimeState(participant_id=participant_id, backend_generation=generation)
    context = RuntimeContext(
        participant_id=participant_id,
        cwd=None,
        io=FakeRuntimeIO(state),
        backend_generation=generation,
        endpoint=f"unix:///tmp/{participant_id}.sock",
        config_path=Path(f"/tmp/{participant_id}-config.json"),
    )
    runtime = FakeRuntime(context)
    runtime.state.backend_alive = True
    return runtime


def wrap_runtime(base: FakeRuntime, wrapper_cls: type[FakeRuntime]) -> FakeRuntime:
    """The same shared state behind a runtime subclass with canned behaviour."""
    wrapped = wrapper_cls(base.context)
    wrapped.state = base.state
    return wrapped


class RecordingGates:
    """All the injected gates, recording what the service asked of them."""

    def __init__(self):
        self.authorized: list[tuple[str, str, str]] = []
        self.absence_checks: list[str] = []
        self.preflights: list[str] = []
        self.copy_mode_checks: list[str] = []
        self.busy_checks: list[str] = []
        self.prompt_checks: list[str] = []
        self.settings_checks: list[tuple[str | None, str | None]] = []
        self.delivered: list[tuple[str, str]] = []
        self.refuse_dispatch_callers: set[str] = set()
        self.refuse_preflight_for: set[str] = set()
        #: Participants whose presence gate raises a temporary refusal.
        self.presence_refusals: set[str] = set()
        #: Participants whose copy-mode check raises a temporary refusal.
        self.copy_mode_refusals: set[str] = set()
        #: Participants whose legacy busy check raises a temporary refusal.
        self.busy_refusals: set[str] = set()
        #: Callers the authorize gate refuses for every action.
        self.refuse_authorize_for: set[str] = set()

    def gates(self) -> ControlGates:
        async def require_absent(participant_id: str) -> None:
            check_absent(participant_id)

        def check_absent(participant_id: str) -> None:
            self.absence_checks.append(participant_id)
            if participant_id in self.presence_refusals:
                raise HumanPresent(f"human focus protects {participant_id!r}")

        async def send_preflight(participant_id: str) -> None:
            self.preflights.append(participant_id)
            if participant_id in self.refuse_preflight_for:
                raise StaleTarget(f"pane of {participant_id!r} no longer exists")

        async def legacy_copy_mode_check(participant_id: str) -> None:
            self.copy_mode_checks.append(participant_id)
            if participant_id in self.copy_mode_refusals:
                raise Busy(f"pane of {participant_id!r} is in copy mode")

        async def legacy_busy_check(participant_id: str) -> None:
            self.busy_checks.append(participant_id)
            if participant_id in self.busy_refusals:
                raise Busy(f"pane of {participant_id!r} is busy with an active job")

        async def legacy_deliver(participant_id: str, prompt: str) -> None:
            self.delivered.append((participant_id, prompt))

        def authorize(participant_id: str, caller_id: str, action: str) -> None:
            self.authorized.append((participant_id, caller_id, action))
            if caller_id in self.refuse_authorize_for:
                raise NotYourChild(
                    f"refusing to control {participant_id!r} for {caller_id!r}: "
                    "only the direct parent or an operator may"
                )
            if action == "queue_dispatch" and caller_id in self.refuse_dispatch_callers:
                raise NotYourChild(
                    f"refusing to dispatch a queued followup of {participant_id!r} "
                    f"for {caller_id!r}: that caller no longer owns the participant"
                )

        def check_prompt(prompt: str) -> None:
            self.prompt_checks.append(prompt)

        def check_settings(model: str | None, reasoning_effort: str | None) -> None:
            self.settings_checks.append((model, reasoning_effort))

        def cwd_for(participant_id: str) -> str | None:
            return "/tmp/wk"

        return ControlGates(
            authorize=authorize,
            require_absent=require_absent,
            check_absent=check_absent,
            send_preflight=send_preflight,
            legacy_copy_mode_check=legacy_copy_mode_check,
            legacy_busy_check=legacy_busy_check,
            check_prompt=check_prompt,
            check_settings=check_settings,
            cwd_for=cwd_for,
            legacy_deliver=legacy_deliver,
        )


class SpyJobs(JobManager):
    """Records finish calls; optionally asserts evidence-before-finish."""

    def __init__(self, store: Store):
        super().__init__(store)
        self.store = store
        self.finishes: list[tuple[str, str]] = []
        #: job handle -> evidence key that must be committed when finish runs.
        self.evidence_gate: dict[str, tuple[str, int, str, str]] = {}

    def finish(self, handle: str, **kwargs) -> Job | None:
        if handle in self.evidence_gate:
            participant_id, generation, session, turn = self.evidence_gate[handle]
            evidence = self.store.get_native_terminal_evidence(
                participant_id=participant_id,
                backend_generation=generation,
                native_session_id=session,
                native_turn_id=turn,
            )
            assert evidence is not None, "job finish ran before the evidence commit"
        self.finishes.append((handle, str(kwargs.get("state"))))
        return super().finish(handle, **kwargs)


class Harness:
    """One control service over one store, with per-participant fake runtimes."""

    def __init__(self, store: Store, participants: dict[str, FakeRuntime]):
        self.store = store
        self.jobs = SpyJobs(store)
        self.runtimes = dict(participants)
        self.gates_recorder = RecordingGates()
        self.service = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=self.runtimes.get,
            gates=self.gates_recorder.gates(),
        )


async def open_harness(store: Store, *participant_ids: str) -> Harness:
    harness = Harness(store, {pid: make_runtime(pid) for pid in participant_ids})
    for runtime in harness.runtimes.values():
        await runtime.open_session(mode=SessionOpenMode.NEW)
    return harness


def state_of(harness: Harness, participant_id: str) -> FakeRuntimeState:
    return harness.runtimes[participant_id].state


async def queue_pending(harness: Harness, prompts: list[str], *, caller_id="caller"):
    """Queue followups that stay pending: busy while queued, then idle."""
    state = state_of(harness, "p1")
    state.native_turn_id = BUSY_TURN
    try:
        jobs = [
            await harness.service.queue_followup("p1", caller_id=caller_id, prompt=prompt)
            for prompt in prompts
        ]
        await drain()
    finally:
        state.native_turn_id = None
    return jobs


def _outcome(
    state: FakeRuntimeState,
    *,
    terminal: NativeTurnTerminal = NativeTurnTerminal.COMPLETED,
    result: str | None = "the answer",
    turn: str | None = None,
    session: str | None = None,
) -> NativeTurnOutcome:
    return NativeTurnOutcome(
        native_session_id=session or state.native_session_id,
        native_turn_id=turn or state.native_turn_id,
        terminal=terminal,
        result=result,
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )


def _evidence_row(state: FakeRuntimeState, turn: str, *, terminal=NativeTurnTerminal.COMPLETED):
    return NativeTerminalEvidence(
        participant_id=state.participant_id,
        backend_generation=state.backend_generation,
        native_session_id=state.native_session_id,
        native_turn_id=turn,
        terminal=terminal,
        result="the answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
        recorded_at=now(),
    )


def _make_operation(
    operation_id: str,
    *,
    participant_id: str = "p1",
    job_handle: str | None,
    kind: ControlKind,
    transport: ControlTransport,
    phase: ControlDeliveryPhase = ControlDeliveryPhase.RESERVED,
    backend_generation: int | None = 1,
    native_session_id: str | None = None,
    native_turn_id: str | None = None,
) -> ControlOperation:
    return ControlOperation(
        operation_id=operation_id,
        participant_id=participant_id,
        kind=kind,
        transport=transport,
        delivery_phase=phase,
        job_handle=job_handle,
        backend_generation=backend_generation,
        native_session_id=native_session_id,
        native_turn_id=native_turn_id,
        created_at=now(),
        updated_at=now(),
    )


async def drain() -> None:
    """Let scheduled dispatch tasks run through their first awaits."""
    for _ in range(4):
        await asyncio.sleep(0)


async def wait_until(predicate, *, attempts: int = 200) -> None:
    """Bounded scheduler assertion without invoking a private dispatcher."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate(), "timed out waiting for daemon-owned control maintenance"


def touch_rows(store: Store, job_handle: str) -> list:
    return list(
        store.conn.execute(
            select(touch_table).where(touch_table.c.job_handle == job_handle)
        ).fetchall()
    )


def operation_rows(store: Store, participant_id: str, kind: ControlKind) -> list:
    return [
        dict(row._mapping)
        for row in store.conn.execute(
            select(control_operations_table)
            .where(control_operations_table.c.participant_id == participant_id)
            .where(control_operations_table.c.kind == str(kind))
        ).fetchall()
    ]


# ---- ordinary send: reservation and receipt transitions --------------------


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
        "p1", backend_generation=state.backend_generation, outcome=_outcome(state)
    )
    assert finished is not None
    assert finished.handle == job.handle
    assert finished.state == JobState.DONE
    assert finished.result == "the answer"


async def test_send_rejects_known_busy_targets(store: Store) -> None:
    """Pending interaction, active native turn, and queued work all refuse."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    state.pending_interaction = PENDING_INTERACTION
    try:
        await service.send("p1", caller_id="caller", prompt="nope")
        raise AssertionError("pending interaction must refuse the send")
    except AwaitingDecision:
        pass
    state.pending_interaction = None

    state.native_turn_id = "turn-human"
    try:
        await service.send("p1", caller_id="caller", prompt="nope")
        raise AssertionError("an active native turn must refuse the send")
    except Busy:
        pass

    # An ordinary send cannot jump ahead of a queued followup.
    state.native_turn_id = None
    await service.queue_followup("p1", caller_id="caller", prompt="first")
    state.native_turn_id = BUSY_TURN  # pin the queue; an idle head would dispatch
    await drain()
    try:
        await service.send("p1", caller_id="caller", prompt="jumper")
        raise AssertionError("an ordinary send must not jump a queued followup")
    except Busy as exc:
        assert "queued followup" in str(exc)
    state.native_turn_id = None
    assert state.sent == []  # nothing was delivered


async def test_active_without_turn_id_rejects_send_and_defers_followup(
    store: Store, monkeypatch
) -> None:
    """ACTIVE is authoritative busy even while a plugin has no turn id yet."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    state.execution_state = RuntimeExecutionState.ACTIVE
    state.native_turn_id = None

    monkeypatch.setattr(control_service_module, "CONTROL_MAINTENANCE_INTERVAL_SECONDS", 0.002)
    service.start(["p1"])
    try:
        try:
            await service.send("p1", caller_id="caller", prompt="must not inject")
            raise AssertionError("known ACTIVE without a turn id must reject ordinary send")
        except Busy as exc:
            assert "without a reported turn id" in str(exc)
        assert state.sent == []

        queued = await service.queue_followup("p1", caller_id="caller", prompt="later")
        scheduled = service._dispatch_tasks.get("p1")
        assert scheduled is not None
        await scheduled

        assert state.sent == []
        assert store.get_job(queued.handle).state == JobState.RUNNING
        assert [operation.job_handle for operation in store.queued_control_operations("p1")] == [
            queued.handle
        ]

        # The per-participant maintainer, not this test, observes the later
        # authoritative IDLE state and dispatches the queued FIFO head.
        state.execution_state = RuntimeExecutionState.IDLE
        await wait_until(lambda: state.sent == ["later"])
    finally:
        await service.aclose()
    assert service.owned_tasks == ()


async def test_unknown_execution_state_never_proves_idle(store: Store) -> None:
    """A live session with UNKNOWN state cannot make a prompt delivery safe."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    state.execution_state = RuntimeExecutionState.UNKNOWN

    try:
        await harness.service.send("p1", caller_id="caller", prompt="must not inject")
        raise AssertionError("UNKNOWN must not be treated as idle")
    except Busy as exc:
        assert "UNKNOWN is not proof of idle" in str(exc)
    assert state.sent == []


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

    monkeypatch.setattr(control_service_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.05)
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

        await wait_until(lambda: store.get_job(first.handle).state == JobState.CRASHED)
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
    assert store.record_native_terminal_evidence(_evidence_row(state, turn)) is True
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
        outcome=_outcome(state, turn=absorb_turn),
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
        outcome=_outcome(state, turn=shared_turn),
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


async def test_legacy_send_keeps_receipt_transitions(store: Store) -> None:
    """A participant without a runtime: same durable phases, tmux delivery."""
    recorder = RecordingGates()
    service = ControlService(
        store=store,
        jobs=JobManager(store),
        runtime_for=lambda participant_id: None,
        gates=recorder.gates(),
    )

    job = await service.send("p1", caller_id="caller", prompt="legacy prompt")

    assert recorder.preflights == ["p1"]
    assert recorder.busy_checks == ["p1"]
    assert recorder.delivered == [("p1", "legacy prompt")]
    (op,) = store.control_operations_for_job(job.handle)
    assert op.transport is ControlTransport.LEGACY_TMUX
    assert op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert op.delivery_result is DeliveryResult.ACCEPTED
    assert job.state == JobState.RUNNING
    assert [j.handle for j in service.active_jobs("p1")] == [job.handle]


async def test_legacy_send_delivery_failure_closes_the_job(store: Store) -> None:
    """tmux failing to type means nothing was delivered: immediate close."""

    async def failing_deliver(participant_id: str, prompt: str) -> None:
        raise RuntimeError("tmux write failed")

    gates = ControlGates(
        authorize=lambda participant_id, caller_id, action: None,
        require_absent=_noop_preflight,
        check_absent=lambda participant_id: None,
        send_preflight=_noop_preflight,
        legacy_copy_mode_check=_noop_preflight,
        legacy_busy_check=_noop_preflight,
        check_prompt=lambda prompt: None,
        check_settings=lambda model, reasoning: None,
        cwd_for=lambda participant_id: None,
        legacy_deliver=failing_deliver,
    )
    service = ControlService(
        store=store, jobs=JobManager(store), runtime_for=lambda pid: None, gates=gates
    )

    try:
        await service.send("p1", caller_id="caller", prompt="never typed")
        raise AssertionError("the delivery failure must propagate")
    except RuntimeError:
        pass

    (op_row,) = operation_rows(store, "p1", ControlKind.SEND)
    finished = store.get_job(op_row["job_handle"])
    assert finished.state == JobState.CRASHED
    assert finished.error_code == "send_failed"
    (op,) = store.control_operations_for_job(finished.handle)
    assert op.delivery_result is DeliveryResult.REJECTED


async def _noop_preflight(participant_id: str) -> None:
    return None


# ---- per-participant serialization ------------------------------------------


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


# ---- steering ----------------------------------------------------------------


async def test_steer_amends_the_exact_current_job(store: Store) -> None:
    """Steering sends the exact expected turn and never creates a job."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="original")
    turn = state.native_turn_id

    amended = await service.steer("p1", caller_id="caller", prompt="more detail")

    assert amended.handle == job.handle  # same job handle, no new job
    assert amended.prompt == "original"  # original prompt preserved
    assert amended.response_format is None
    assert state.steered == [(turn, "more detail")]
    operations = store.control_operations_for_job(job.handle)
    assert [op.kind for op in operations] == [ControlKind.SEND, ControlKind.STEER]
    steer_op = operations[1]
    assert steer_op.native_turn_id == turn
    assert steer_op.payload == "more detail"
    assert steer_op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert steer_op.delivery_result is DeliveryResult.ACCEPTED


async def test_steer_rejects_stale_and_missing_turns(store: Store) -> None:
    """No active turn, a human-only turn, and a stale turn all stay refusals."""

    class StaleRefusingRuntime(FakeRuntime):
        async def steer(
            self, *, operation_id: str, native_turn_id: str, prompt: str
        ) -> ControlReceipt:
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.REJECTED,
                error_code="stale_turn",
                error="expectedTurnId no longer active",
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), StaleRefusingRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service

    # No active turn at all.
    try:
        await service.steer("p1", caller_id="caller", prompt="amend")
        raise AssertionError("steering without an active turn must refuse")
    except StaleTarget:
        pass

    # A human-only turn: no Theater job owns it, so no synthetic job is created.
    state.native_turn_id = "turn-human"
    try:
        await service.steer("p1", caller_id="caller", prompt="amend")
        raise AssertionError("steering a human-only turn must refuse")
    except StaleTarget as exc:
        assert "no Theater job" in str(exc)
    assert store.running_jobs_for_target("p1") == []
    state.native_turn_id = None

    # A stale turn: the refusal is final, never reinterpreted as send or queue.
    job = await service.send("p1", caller_id="caller", prompt="original")
    try:
        await service.steer("p1", caller_id="caller", prompt="amend")
        raise AssertionError("a stale-turn refusal must propagate")
    except StaleTarget as exc:
        assert "stale_turn" in str(exc)
    assert len(store.running_jobs_for_target("p1")) == 1  # no new job handle
    assert state.steered == []
    assert job.state == JobState.RUNNING


async def test_steer_requires_the_expected_job_handle(store: Store) -> None:
    """An explicit expected job that mismatches the turn's job refuses."""
    harness = await open_harness(store, "p1")
    service = harness.service
    job = await service.send("p1", caller_id="caller", prompt="original")

    try:
        await service.steer("p1", caller_id="caller", prompt="amend", job_handle="someone#else#1")
        raise AssertionError("a mismatched expected job must refuse")
    except StaleTarget:
        pass
    # Without an expectation, the same steer amends the current job.
    amended = await service.steer("p1", caller_id="caller", prompt="amend")
    assert amended.handle == job.handle


# ---- queued followups ----------------------------------------------------------


async def test_queue_followup_creates_awaitable_job_with_persisted_fifo(store: Store) -> None:
    """The job exists immediately; positions come from the persisted allocator."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    first = await service.send("p1", caller_id="caller", prompt="first")  # turn active
    followups = [
        await service.queue_followup("p1", caller_id="caller", prompt=f"followup {i}")
        for i in range(3)
    ]

    assert all(job.state == JobState.RUNNING for job in followups)
    queued = store.queued_control_operations("p1")
    assert [op.queue_sequence for op in queued] == sorted(op.queue_sequence for op in queued)
    assert [op.job_handle for op in queued] == [job.handle for job in followups]
    assert all(op.transport is ControlTransport.NATIVE_RUNTIME for op in queued)
    # The prompt is already persisted on the awaitable job.
    assert store.get_job(followups[0].handle).prompt == "followup 0"
    # A pending followup is excluded from the active-job selectors.
    assert [job.handle for job in service.active_jobs("p1")] == [first.handle]
    assert [job.handle for job in service.queued_jobs("p1")] == [job.handle for job in followups]
    del state


async def test_queue_dispatches_fifo_one_at_a_time(store: Store) -> None:
    """One prompt at a time, in queue order, after an authoritative idle check."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    await service.send("p1", caller_id="caller", prompt="first")
    first_turn = state.native_turn_id
    handles = [
        job.handle for job in await queue_pending(harness, [f"followup {i}" for i in range(3)])
    ]

    # Busy: the whole pass defers, items stay queued.
    state.native_turn_id = "turn-human"
    outcome = await service.dispatch_queue("p1")
    assert outcome.deferred is True
    assert outcome.dispatched == ()
    assert len(store.queued_control_operations("p1")) == 3
    state.native_turn_id = None

    # The turn completes; the evidence path schedules the next dispatch.
    await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=first_turn),
    )
    await drain()
    assert state.sent == ["first", "followup 0"]
    assert store.get_job(handles[0]).state == JobState.RUNNING
    assert len(store.queued_control_operations("p1")) == 2

    # Each completion dispatches exactly the next one, in FIFO order.
    second_turn = state.native_turn_id
    await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=second_turn),
    )
    state.native_turn_id = None  # the completed turn no longer reports busy
    await drain()
    assert state.sent == ["first", "followup 0", "followup 1"]
    assert len(store.queued_control_operations("p1")) == 1


async def test_queue_bound_is_enforced_before_creation(store: Store) -> None:
    """The 32-pending bound refuses the 33rd followup before creating a job."""
    from theater.constants.daemon import CONTROL_QUEUE_MAX_PENDING

    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    state.native_turn_id = BUSY_TURN  # keep every item queued, none dispatch
    service = harness.service

    for i in range(CONTROL_QUEUE_MAX_PENDING):
        await service.queue_followup("p1", caller_id="caller", prompt=f"item {i}")
    await drain()
    assert store.queued_control_operation_count("p1") == CONTROL_QUEUE_MAX_PENDING

    try:
        await service.queue_followup("p1", caller_id="caller", prompt="one too many")
        raise AssertionError("the queue bound must refuse the next followup")
    except Busy as exc:
        assert "queued followups" in str(exc)
    assert store.queued_control_operation_count("p1") == CONTROL_QUEUE_MAX_PENDING
    state.native_turn_id = None


async def test_queue_revalidates_authorization_at_dispatch(store: Store) -> None:
    """Lost ownership finishes that item explicitly; the queue continues."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    recorder = harness.gates_recorder

    state.native_turn_id = BUSY_TURN  # keep both items queued, none dispatch
    try:
        bad = await service.queue_followup("p1", caller_id="x", prompt="bad caller")
        good = await service.queue_followup("p1", caller_id="y", prompt="good caller")
        await drain()
    finally:
        state.native_turn_id = None
    recorder.refuse_dispatch_callers.add("x")

    outcome = await service.dispatch_queue("p1")

    assert [handle for handle, _ in outcome.failed] == [bad.handle]
    assert store.get_job(bad.handle).state == JobState.CRASHED
    assert store.get_job(bad.handle).error_code == "not_your_child"
    assert outcome.dispatched == (good.handle,)
    assert store.get_job(good.handle).state == JobState.RUNNING
    assert state.sent == ["good caller"]


async def test_queue_temporary_conditions_leave_items_queued(store: Store) -> None:
    """Busy and pending native interaction defer the head, never fail it."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    (job,) = await queue_pending(harness, ["waiting"])

    for condition in (
        "busy_turn",
        "pending_interaction",
    ):
        if condition == "busy_turn":
            state.native_turn_id = "turn-human"
        else:
            state.pending_interaction = PENDING_INTERACTION
        outcome = await service.dispatch_queue("p1")
        assert outcome.deferred is True
        assert outcome.failed == ()
        assert store.get_job(job.handle).state == JobState.RUNNING
        assert len(store.queued_control_operations("p1")) == 1
        state.native_turn_id = None
        state.pending_interaction = None

    # Idle again: the item dispatches.
    outcome = await service.dispatch_queue("p1")
    assert outcome.dispatched == (job.handle,)
    assert state.sent == ["waiting"]


async def test_queue_definitive_preflight_failure_finishes_the_item(store: Store) -> None:
    """A dead target finishes its queued item with an explicit error."""
    harness = await open_harness(store, "p1")
    recorder = harness.gates_recorder
    service = harness.service

    (job,) = await queue_pending(harness, ["doomed"])
    recorder.refuse_preflight_for.add("p1")

    outcome = await service.dispatch_queue("p1")

    assert [handle for handle, _ in outcome.failed] == [job.handle]
    finished = store.get_job(job.handle)
    assert finished.state == JobState.CRASHED
    assert finished.error_code == "stale_target"
    assert "pane" in finished.result
    (op,) = store.control_operations_for_job(job.handle)
    assert op.delivery_result is DeliveryResult.REJECTED


async def test_pending_queued_jobs_receive_no_results_or_touches(store: Store) -> None:
    """Queued jobs get no transcript/native results and no path touches."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    jobs = harness.jobs

    active = await service.send("p1", caller_id="caller", prompt="active work")
    active_turn = state.native_turn_id
    (queued,) = await queue_pending(harness, ["later work"])
    paths = (EventPath(path="notes.txt", mode="write"),)

    # An observer feeding path events cannot attribute anything to the
    # pending followup: it has no accumulator until dispatch.
    jobs.observe_paths(active.handle, paths)
    jobs.observe_paths(queued.handle, paths)

    # A human turn completing mid-queue cannot finish the queued job either.
    human = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn="turn-human"),
    )
    assert human is None  # no Theater job owns the human turn
    assert store.get_job(queued.handle).state == JobState.RUNNING

    # Completing the active job writes touches for it — and none for the
    # pending followup, which has no accumulator.
    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=active_turn),
    )
    assert finished is not None and finished.handle == active.handle
    assert len(touch_rows(store, active.handle)) == 1
    assert touch_rows(store, queued.handle) == []

    # Interrupt the queue; the cancelled followup still receives no touches.
    await service.interrupt("p1", caller_id="caller")
    killed = store.get_job(queued.handle)
    assert killed.state == JobState.KILLED
    assert touch_rows(store, queued.handle) == []


async def test_dispatch_attaches_the_touch_accumulator(store: Store) -> None:
    """A dispatched followup becomes a real active job and records touches."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    jobs = harness.jobs

    await service.send("p1", caller_id="caller", prompt="active work")
    first_turn = state.native_turn_id
    (queued,) = await queue_pending(harness, ["later work"])

    # The active turn completes; the evidence path dispatches the followup.
    await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=first_turn),
    )
    await drain()
    assert state.sent == ["active work", "later work"]
    assert [job.handle for job in service.active_jobs("p1")] == [queued.handle]

    # Now the dispatched followup accepts path attribution like any active job.
    jobs.observe_paths(queued.handle, (EventPath(path="notes.txt", mode="write"),))
    second_turn = state.native_turn_id
    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=second_turn),
    )
    assert finished is not None and finished.handle == queued.handle
    assert finished.state == JobState.DONE
    assert len(touch_rows(store, queued.handle)) == 1


# ---- interrupt -----------------------------------------------------------------


async def test_interrupt_cancels_queue_and_requests_exact_turn(store: Store) -> None:
    """Interrupt cancels every undelivered followup and the exact active turn."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    active = await service.send("p1", caller_id="caller", prompt="active work")
    active_turn = state.native_turn_id
    queued = await queue_pending(harness, ["followup 0", "followup 1"])
    state.native_turn_id = active_turn  # the active turn is still running

    outcome = await service.interrupt("p1", caller_id="caller")

    assert outcome.interrupted is True
    assert set(outcome.cancelled_followups) == {job.handle for job in queued}
    assert state.interrupted == [active_turn]  # the exact turn, not a guess
    for job in queued:
        assert store.get_job(job.handle).state == JobState.KILLED
        assert store.get_job(job.handle).error_code == "interrupted"
    # The active job is NOT finished by the request — only evidence does that.
    assert store.get_job(active.handle).state == JobState.RUNNING
    assert store.queued_control_operations("p1") == []

    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(
            state, turn=active_turn, terminal=NativeTurnTerminal.INTERRUPTED, result=None
        ),
    )
    assert finished is not None and finished.handle == active.handle
    assert finished.state == JobState.KILLED
    assert finished.error_code == "interrupted"
    # The interrupted turn also cancelled anything queued after it (none left).
    assert store.queued_control_operations("p1") == []


async def test_interrupt_while_idle_still_clears_the_queue(store: Store) -> None:
    """An interruption request while already idle cancels pending followups."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    # Pending interaction keeps the queue from dispatching while we set up.
    state.pending_interaction = PENDING_INTERACTION
    queued = [
        await service.queue_followup("p1", caller_id="caller", prompt=f"followup {i}")
        for i in range(2)
    ]
    await drain()
    state.pending_interaction = None  # idle now — but nothing auto-dispatches
    # before the interrupt below takes the lock first (the scheduled dispatch
    # from queue_followup already returned, deferred, while pinned busy).

    outcome = await service.interrupt("p1", caller_id="caller")

    assert outcome.interrupted is False
    assert outcome.reason == "already_idle"
    assert set(outcome.cancelled_followups) == {job.handle for job in queued}
    assert state.interrupted == []  # no turn to interrupt, nothing requested
    assert state.sent == []  # nothing was ever delivered
    for job in queued:
        assert store.get_job(job.handle).state == JobState.KILLED
    assert store.queued_control_operations("p1") == []


async def test_interrupt_racing_queue_dispatch_cancels_nothing_in_flight(store: Store) -> None:
    """A dispatch in progress is not cancelled; everything behind it is."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class GatedRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            receipt = await super().send(operation_id=operation_id, prompt=prompt)
            entered.set()
            await release.wait()
            return receipt

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), GatedRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service

    first, second = await queue_pending(harness, ["head", "second"])

    dispatch = asyncio.create_task(service.dispatch_queue("p1"))
    await entered.wait()  # the head is mid-transmission, holding the lock
    interrupt = asyncio.create_task(service.interrupt("p1", caller_id="caller"))
    await asyncio.sleep(0)
    assert not interrupt.done()  # the interrupt waits for the dispatch lock
    release.set()
    dispatched_turn = state.native_turn_id  # set by the head's send, pre-interrupt

    dispatch_outcome = await dispatch
    interrupt_outcome = await interrupt

    assert dispatch_outcome.dispatched == (first.handle,)
    assert interrupt_outcome.interrupted is True
    assert interrupt_outcome.cancelled_followups == (second.handle,)
    assert state.sent == ["head"]  # the cancelled item never started
    assert store.get_job(second.handle).state == JobState.KILLED
    # The dispatched head is finished only by its own terminal evidence.
    assert store.get_job(first.handle).state == JobState.RUNNING
    assert state.interrupted == [dispatched_turn]


async def test_interrupt_before_dispatch_prevents_any_start(store: Store) -> None:
    """Interrupt wins the lock: no cancelled item silently starts afterwards."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    queued = await queue_pending(harness, ["followup 0", "followup 1"])

    outcome = await service.interrupt("p1", caller_id="caller")
    after = await service.dispatch_queue("p1")

    assert set(outcome.cancelled_followups) == {job.handle for job in queued}
    assert after.dispatched == () and after.failed == () and after.deferred is False
    assert state.sent == []  # nothing was ever delivered
    for job in queued:
        assert store.get_job(job.handle).state == JobState.KILLED
    assert store.queued_control_operations("p1") == []


async def test_native_ui_interrupt_cancels_pending_followups(store: Store) -> None:
    """A native-UI interruption cancels Theater's queue without sending anything."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    queued = await queue_pending(harness, ["followup 0", "followup 1"])

    cancelled = await service.handle_native_ui_interrupt("p1", native_turn_id="turn-human")

    assert set(cancelled) == {job.handle for job in queued}
    assert state.interrupted == []  # Theater sent no interrupt request
    assert state.sent == []
    for job in queued:
        finished = store.get_job(job.handle)
        assert finished.state == JobState.KILLED
        assert finished.error_code == "interrupted"
    assert store.queued_control_operations("p1") == []


# ---- restart and reconciliation ----------------------------------------------------


async def test_restart_fails_undelivered_followups_and_never_replays(store: Store) -> None:
    """Restart: queued jobs fail with daemon_restarted and are never replayed."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    queued = await queue_pending(harness, ["followup 0", "followup 1"])

    failed = service.fail_undelivered_followups(["p1"])

    assert {job.handle for job in failed} == {job.handle for job in queued}
    for job in queued:
        finished = store.get_job(job.handle)
        assert finished.state == JobState.CRASHED
        assert finished.error_code == "daemon_restarted"
        assert "never replayed" in finished.result
        (op,) = store.control_operations_for_job(job.handle)
        assert op.delivery_result is DeliveryResult.REJECTED
    assert store.queued_control_operations("p1") == []

    # Nothing replays: the queue is empty and the backend never saw a prompt.
    after = await service.dispatch_queue("p1")
    assert after.dispatched == () and after.deferred is False
    assert state.sent == []


async def test_restart_keeps_a_proven_unsent_legacy_queue(store: Store) -> None:
    harness = Harness(store, {})
    harness.gates_recorder.busy_refusals.add("p1")
    queued = await harness.service.queue_followup("p1", caller_id="caller", prompt="legacy next")
    await drain()

    failed = harness.service.fail_undelivered_followups(["p1"], preserve_legacy_queued=True)

    assert failed == []
    assert [operation.job_handle for operation in store.queued_control_operations("p1")] == [
        queued.handle
    ]
    harness.gates_recorder.busy_refusals.clear()
    outcome = await harness.service.dispatch_queue("p1")
    assert outcome.dispatched == (queued.handle,)
    assert harness.gates_recorder.delivered == [("p1", "legacy next")]
    event = store.bus_tail()[-1]
    assert event["from_id"] == "caller"
    assert event["to_id"] == "p1"
    assert event["kind"] == "agent.send"
    assert event["payload"] == {"handle": queued.handle, "prompt": "legacy next"}


async def test_restart_fails_reserved_never_dispatched_sends(store: Store) -> None:
    """A send reserved before transmission never began: it fails too."""
    harness = await open_harness(store, "p1")
    runtime = harness.runtimes["p1"]
    state = runtime.state
    del state

    job = harness.jobs.create(
        handle="p1#resv",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="reserved only",
        cwd=None,
    )
    store.reserve_control_operation(
        _make_operation(
            "p1#resv:send",
            job_handle=job.handle,
            kind=ControlKind.SEND,
            transport=ControlTransport.NATIVE_RUNTIME,
        )
    )

    failed = harness.service.fail_undelivered_followups(["p1"])

    assert [j.handle for j in failed] == [job.handle]
    finished = store.get_job(job.handle)
    assert finished.state == JobState.CRASHED
    assert finished.error_code == "daemon_restarted"
    (op,) = store.control_operations_for_job(job.handle)
    assert op.delivery_result is DeliveryResult.REJECTED


async def test_crash_between_evidence_and_finish_recovers_exactly_once(store: Store) -> None:
    """Evidence committed, job still running: restart finishes it once."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    jobs = harness.jobs

    job = await service.send("p1", caller_id="caller", prompt="recoverable")
    turn = state.native_turn_id
    # Simulate the crash: the evidence commit landed, the finish did not.
    assert store.record_native_terminal_evidence(_evidence_row(state, turn))
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
        "p1", backend_generation=state.backend_generation, outcome=_outcome(state)
    )
    assert finished is not None and finished.state == JobState.DONE
    # The gate inside SpyJobs.finish asserted the evidence was already there.


async def test_duplicate_and_delayed_evidence_finishes_once(store: Store) -> None:
    """Repeated evidence never finishes twice or rewrites terminal state."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="once only")
    outcome = _outcome(state)
    first = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=outcome
    )
    second = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=outcome
    )
    late = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, result="late rewrite attempt"),
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
        outcome=_outcome(state, session="thread-somebody-else"),
    )
    assert wrong_session is None
    assert store.get_job(job.handle).state == JobState.RUNNING

    wrong_turn = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn="turn-not-mine"),
    )
    assert wrong_turn is None
    assert store.get_job(job.handle).state == JobState.RUNNING

    exact = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=_outcome(state)
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
        _make_operation(
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
        "p1", backend_generation=state.backend_generation, outcome=_outcome(state)
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
    store.record_native_terminal_evidence(_evidence_row(state, op.native_turn_id))
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


# ---- settings ------------------------------------------------------------------


async def test_settings_idle_only_supplied_fields(store: Store) -> None:
    """Idle settings update: only supplied fields, effective after readback."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    recorder = harness.gates_recorder

    outcome = await service.update_settings("p1", caller_id="caller", model="m2")

    assert outcome.applied is True
    assert outcome.model == "m2"  # effective value from the confirmed readback
    assert outcome.reasoning_effort is None  # untouched field stays untouched
    assert state.settings == {"model": "m2"}
    assert recorder.settings_checks == [("m2", None)]
    # The settings operation row carries only the supplied fields — approval
    # and sandbox policy are immutable and have no representation here.
    (settings_row,) = operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE)
    assert settings_row["job_handle"] is None  # settings follow no Theater job
    assert json.loads(settings_row["payload"]) == {"model": "m2"}
    assert settings_row["delivery_result"] == str(DeliveryResult.ACCEPTED)


async def test_settings_rejects_busy_capability_and_empty_requests(store: Store) -> None:
    """Busy targets, gated capabilities, and empty requests all refuse."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    try:
        await service.update_settings("p1", caller_id="caller")
        raise AssertionError("an empty settings request must refuse")
    except BadRequest as exc:
        assert "nothing to update" in str(exc)

    state.unavailable = {
        RuntimeCapability.SETTINGS_UPDATE: CapabilityUnavailableReason.GATED_BY_BACKEND
    }
    try:
        await service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a gated capability must refuse")
    except BadRequest as exc:
        assert "gated_by_backend" in str(exc)
    assert operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE) == []
    state.unavailable = {}

    state.native_turn_id = "turn-active"
    try:
        await service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a busy target must refuse settings updates")
    except Busy:
        pass
    state.native_turn_id = None


async def test_settings_uncertain_delivery_stays_visible(store: Store) -> None:
    """An uncertain settings application reports applied=None, never success."""

    class UncertainRuntime(FakeRuntime):
        async def update_settings(
            self,
            *,
            operation_id: str,
            model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> ControlReceipt:
            return ControlReceipt(operation_id=operation_id, result=DeliveryResult.UNKNOWN)

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), UncertainRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)

    outcome = await harness.service.update_settings("p1", caller_id="caller", model="m2")

    assert outcome.applied is None  # visibly uncertain
    assert outcome.model == "m2"
    (settings_row,) = operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE)
    assert settings_row["delivery_result"] == str(DeliveryResult.UNKNOWN)


# ---- selectors -----------------------------------------------------------------


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


# ---- correction round 1: first-write-wins evidence across the crash window ----


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
    assert store.record_native_terminal_evidence(_evidence_row(state, turn))

    # Conflicting duplicate B arrives while the job still runs.
    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(
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


async def test_duplicate_interrupted_evidence_cancels_queue_from_first_write(
    store: Store,
) -> None:
    """The effective terminal is the persisted one: INTERRUPTED A cancels."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    await service.send("p1", caller_id="caller", prompt="active work")
    turn = state.native_turn_id
    (queued,) = await queue_pending(harness, ["later work"])
    # Persisted first write says INTERRUPTED; the crash window is open.
    assert store.record_native_terminal_evidence(
        _evidence_row(state, turn, terminal=NativeTurnTerminal.INTERRUPTED)
    )

    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=turn, terminal=NativeTurnTerminal.COMPLETED),
    )

    assert finished is not None and finished.state == JobState.KILLED
    assert store.get_job(queued.handle).state == JobState.KILLED


# ---- correction round 1: ambiguity fails the newer job closed --------------------


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
        outcome=_outcome(state, turn=turn),
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
        _make_operation(
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


# ---- correction round 1: reconciliation needs the full current identity ---------


async def test_reconcile_turn_id_collision_on_new_generation_closes_delivery(
    store: Store,
) -> None:
    """An id collision after a generation change must not postpone the close."""

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
    turn = state.native_turn_id
    # The backend relaunched: same turn id, different generation.
    state.backend_generation = state.backend_generation + 1

    resolved = await service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0)

    assert [j.handle for j in resolved] == [job.handle]
    finished = store.get_job(job.handle)
    assert finished.state == JobState.CRASHED
    assert finished.error_code == "delivery_unknown"
    del turn


async def test_reconcile_turn_id_collision_on_new_session_closes_delivery(
    store: Store,
) -> None:
    """An id collision after a session change must not postpone the close."""

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
    turn = state.native_turn_id
    # A new native thread: same turn id, different session.
    state.native_session_id = "thread-reused"

    resolved = await service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0)

    assert [j.handle for j in resolved] == [job.handle]
    assert store.get_job(job.handle).error_code == "delivery_unknown"
    del turn


# ---- correction round 1: a receipt is authoritative only for its operation -----


async def test_mismatched_receipt_id_keeps_send_uncertain(store: Store) -> None:
    """A receipt naming another operation cannot settle this one accepted."""

    class CrossWiredRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id="p1#999:send",
                result=DeliveryResult.ACCEPTED,
                native_turn_id="turn-x",
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), CrossWiredRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="cross-wired")

    (op,) = store.control_operations_for_job(job.handle)
    assert op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert op.delivery_result is DeliveryResult.UNKNOWN  # uncertain, not accepted
    assert op.error_code == "delivery_unknown"
    assert op.native_turn_id is None  # the untrusted receipt's turn is discarded
    assert job.state == JobState.RUNNING
    assert state.sent == ["cross-wired"]  # delivered once, never resent

    # Deadline reconciliation is the only close — no retry, no fallback.
    resolved = await service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0)
    assert [j.handle for j in resolved] == [job.handle]
    assert store.get_job(job.handle).error_code == "delivery_unknown"
    assert state.sent == ["cross-wired"]


async def test_mismatched_receipt_id_keeps_steer_uncertain(store: Store) -> None:
    """A cross-wired steer receipt settles unknown; the job is never resent."""

    class CrossWiredSteer(FakeRuntime):
        async def steer(
            self, *, operation_id: str, native_turn_id: str, prompt: str
        ) -> ControlReceipt:
            await super().steer(
                operation_id=operation_id, native_turn_id=native_turn_id, prompt=prompt
            )
            return ControlReceipt(
                operation_id="p1#999:steer",
                result=DeliveryResult.ACCEPTED,
                native_turn_id=native_turn_id,
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), CrossWiredSteer)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="active work")
    turn = state.native_turn_id
    returned = await service.steer("p1", caller_id="caller", prompt="amend")

    assert returned.handle == job.handle
    assert store.get_job(job.handle).state == JobState.RUNNING
    (steer_op,) = [
        op for op in store.control_operations_for_job(job.handle) if op.kind is ControlKind.STEER
    ]
    assert steer_op.delivery_result is DeliveryResult.UNKNOWN
    assert steer_op.error_code == "delivery_unknown"
    # The amendment reached the backend exactly once; nothing is re-sent.
    assert state.steered == [(turn, "amend")]


async def test_unknown_steer_cannot_deadline_or_overwrite_the_original_job(
    store: Store, monkeypatch
) -> None:
    """Only an uncertain prompt owns a prompt deadline, never its amendment."""

    class UnknownSteer(FakeRuntime):
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

    monkeypatch.setattr(control_service_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(control_service_module, "CONTROL_MAINTENANCE_INTERVAL_SECONDS", 0.002)
    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), UnknownSteer)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service
    service.start(["p1"])
    try:
        original = await service.send("p1", caller_id="caller", prompt="original")
        turn = state.native_turn_id
        amended = await service.steer("p1", caller_id="caller", prompt="uncertain amend")
        queued = await service.queue_followup("p1", caller_id="caller", prompt="after original")

        # The queue keeps daemon-owned maintenance alive past the prompt deadline.
        await asyncio.sleep(0.03)
        assert amended.handle == original.handle
        assert store.get_job(original.handle).state == JobState.RUNNING
        (steer_operation,) = [
            operation
            for operation in store.control_operations_for_job(original.handle)
            if operation.kind is ControlKind.STEER
        ]
        assert steer_operation.delivery_result is DeliveryResult.UNKNOWN

        # Exact terminal evidence is committed before the immutable job finish, then the deferred
        # FIFO head self-progresses without a direct queue/reconciliation call.
        state.native_turn_id = None
        state.execution_state = RuntimeExecutionState.IDLE
        finished = await service.record_terminal_evidence(
            "p1",
            backend_generation=state.backend_generation,
            outcome=_outcome(state, turn=turn),
        )
        assert finished is not None and finished.state == JobState.DONE
        await wait_until(lambda: state.sent == ["original", "after original"])
        assert store.get_job(original.handle).state == JobState.DONE
        assert store.get_job(queued.handle).state == JobState.RUNNING
    finally:
        await service.aclose()


async def test_cancelled_or_crash_left_job_bearing_steers_settle_without_touching_original_job(
    store: Store,
) -> None:
    """An amendment's uncertain delivery never owns its prompt job's fate."""
    entered = asyncio.Event()

    class BlockingSteer(FakeRuntime):
        async def steer(
            self, *, operation_id: str, native_turn_id: str, prompt: str
        ) -> ControlReceipt:
            self.state.steered.append((native_turn_id, prompt))
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("the cancelled steer must never resume")

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), BlockingSteer)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    service = harness.service
    state = state_of(harness, "p1")
    original = await service.send("p1", caller_id="caller", prompt="original prompt")
    original_turn = state.native_turn_id
    assert original_turn is not None

    cancelled = asyncio.create_task(service.steer("p1", caller_id="caller", prompt="amend"))
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    (cancelled_steer,) = [
        operation
        for operation in store.control_operations_for_job(original.handle)
        if operation.kind is ControlKind.STEER
    ]
    assert cancelled_steer.delivery_phase is ControlDeliveryPhase.SETTLED
    assert cancelled_steer.delivery_result is DeliveryResult.UNKNOWN
    assert cancelled_steer.execution_barrier is False
    assert store.get_job(original.handle).state == JobState.RUNNING

    # Model the remaining hard-crash window: a job-bearing amendment crossed its write but the
    # process died before its cancellation handler could settle it.
    crashed_id = "p1#crash-left:steer"
    store.reserve_control_operation(
        _make_operation(
            crashed_id,
            job_handle=original.handle,
            kind=ControlKind.STEER,
            transport=ControlTransport.NATIVE_RUNTIME,
            native_session_id=state.native_session_id,
            native_turn_id=original_turn,
        )
    )
    store.mark_control_operation_dispatched(
        crashed_id,
        native_session_id=state.native_session_id,
        native_turn_id=original_turn,
        updated_at=now(),
    )

    assert service.fail_undelivered_followups(["p1"]) == []
    crash_left = store.get_control_operation(crashed_id)
    assert crash_left is not None
    assert crash_left.delivery_phase is ControlDeliveryPhase.SETTLED
    assert crash_left.delivery_result is DeliveryResult.UNKNOWN
    assert crash_left.execution_barrier is False
    assert store.get_job(original.handle).state == JobState.RUNNING
    assert await service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0) == []
    assert store.get_job(original.handle).state == JobState.RUNNING

    # Both uncertain amendments are retention-eligible even though the original prompt is still
    # running: only prompt operations retain a job's recovery obligation.
    assert store.prune_control_operations(older_than=now() + 1.0) == 2
    assert store.get_control_operation(cancelled_steer.operation_id) is None
    assert store.get_control_operation(crashed_id) is None

    state.native_turn_id = None
    state.execution_state = RuntimeExecutionState.IDLE
    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=original_turn),
    )
    assert finished is not None and finished.state == JobState.DONE
    assert store.get_job(original.handle).state == JobState.DONE


async def test_mismatched_receipt_id_keeps_settings_uncertain(store: Store) -> None:
    """A cross-wired settings receipt reports applied=None, honestly."""

    class CrossWiredSettings(FakeRuntime):
        async def update_settings(
            self,
            *,
            operation_id: str,
            model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> ControlReceipt:
            await super().update_settings(
                operation_id=operation_id, model=model, reasoning_effort=reasoning_effort
            )
            return ControlReceipt(
                operation_id="p1#999:settings_update",
                result=DeliveryResult.ACCEPTED,
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), CrossWiredSettings)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    service = harness.service

    outcome = await service.update_settings("p1", caller_id="caller", model="m2")

    assert outcome.applied is None  # explicitly uncertain, never assumed
    assert outcome.error_code == "delivery_unknown"
    (op,) = operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE)
    assert op["delivery_result"] == str(DeliveryResult.UNKNOWN)
    assert op["error_code"] == "delivery_unknown"


async def test_mismatched_receipt_id_keeps_interrupt_uncertain(store: Store) -> None:
    """A cross-wired interrupt receipt reports interrupted=False, uncertain."""

    class CrossWiredInterrupt(FakeRuntime):
        async def interrupt(
            self, *, operation_id: str, native_turn_id: str | None = None
        ) -> ControlReceipt:
            await super().interrupt(operation_id=operation_id, native_turn_id=native_turn_id)
            return ControlReceipt(
                operation_id="p1#999:interrupt",
                result=DeliveryResult.ACCEPTED,
                native_turn_id=native_turn_id,
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), CrossWiredInterrupt)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    service = harness.service

    await service.send("p1", caller_id="caller", prompt="active work")
    outcome = await service.interrupt("p1", caller_id="caller")

    assert outcome.interrupted is False
    assert outcome.reason == "delivery_unknown"
    (op,) = operation_rows(store, "p1", ControlKind.INTERRUPT)
    assert op["delivery_result"] == str(DeliveryResult.UNKNOWN)
    assert op["error_code"] == "delivery_unknown"


# ---- correction round 1: accepted without a turn id stays uncertain ---------------


async def test_accepted_receipt_without_turn_id_stays_uncertain(store: Store) -> None:
    """An accepted prompt with no native turn id can never correlate: uncertain."""

    class TurnlessRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.ACCEPTED,
                native_turn_id=None,
            )

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), TurnlessRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    state = state_of(harness, "p1")
    service = harness.service

    job = await service.send("p1", caller_id="caller", prompt="turnless")

    (op,) = store.control_operations_for_job(job.handle)
    assert op.delivery_result is DeliveryResult.UNKNOWN  # not accepted: uncertain
    assert op.native_turn_id is None
    assert job.state == JobState.RUNNING  # no immortal accepted job
    assert state.sent == ["turnless"]  # never resent

    # The ambiguous-delivery deadline is the only close.
    resolved = await service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0)
    assert [j.handle for j in resolved] == [job.handle]
    assert store.get_job(job.handle).error_code == "delivery_unknown"


# ---- correction round 1: capability gates at execution ---------------------------


async def test_capability_gates_refuse_with_recorded_reason(store: Store) -> None:
    """SEND/STEER/INTERRUPT fail closed with the recorded reason."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    # SEND refused, with the recorded reason, before anything is reserved.
    state.unavailable[RuntimeCapability.SEND] = CapabilityUnavailableReason.GATED_BY_BACKEND
    try:
        await service.send("p1", caller_id="caller", prompt="nope")
        raise AssertionError("a gated send must be refused")
    except BadRequest as exc:
        assert "gated_by_backend" in str(exc)
    assert operation_rows(store, "p1", ControlKind.SEND) == []
    # The queue is Theater-owned but its native delivery is a send: with SEND
    # unavailable the reservation is refused too, before the slot is spent.
    try:
        await service.queue_followup("p1", caller_id="caller", prompt="later")
        raise AssertionError("a queue reservation without SEND must be refused")
    except BadRequest as exc:
        assert "gated_by_backend" in str(exc)
    assert store.queued_control_operation_count("p1") == 0
    assert operation_rows(store, "p1", ControlKind.QUEUE_FOLLOWUP) == []
    state.unavailable.clear()

    job = await service.send("p1", caller_id="caller", prompt="active work")

    state.unavailable[RuntimeCapability.STEER] = (
        CapabilityUnavailableReason.UNSUPPORTED_NATIVE_VERSION
    )
    try:
        await service.steer("p1", caller_id="caller", prompt="amend")
        raise AssertionError("a gated steer must be refused")
    except BadRequest as exc:
        assert "unsupported_native_version" in str(exc)
    assert operation_rows(store, "p1", ControlKind.STEER) == []

    state.unavailable.clear()
    state.unavailable[RuntimeCapability.INTERRUPT] = CapabilityUnavailableReason.SESSION_STATE
    try:
        await service.interrupt("p1", caller_id="caller")
        raise AssertionError("a gated interrupt must be refused")
    except BadRequest as exc:
        assert "session_state" in str(exc)
    assert operation_rows(store, "p1", ControlKind.INTERRUPT) == []
    state.unavailable.clear()
    del job


async def test_queue_capability_lost_at_dispatch_fails_item_with_reason(
    store: Store,
) -> None:
    """SEND is revalidated at dispatch; a lost one fails the item."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    state.native_turn_id = BUSY_TURN  # keep the item queued
    try:
        job = await service.queue_followup("p1", caller_id="caller", prompt="later work")
        await drain()
    finally:
        state.native_turn_id = None
    assert store.queued_control_operation_count("p1") == 1

    # SEND disappeared between reservation and dispatch: definitive failure.
    state.unavailable[RuntimeCapability.SEND] = CapabilityUnavailableReason.SESSION_STATE
    outcome = await service.dispatch_queue("p1")

    assert [handle for handle, _ in outcome.failed] == [job.handle]
    finished = store.get_job(job.handle)
    assert finished.state == JobState.CRASHED
    assert "session_state" in (finished.result or "")
    (op,) = store.control_operations_for_job(job.handle)
    assert op.delivery_result is DeliveryResult.REJECTED
    assert "session_state" in (op.error or "")
    # The queue is drained: nothing queued, nothing dispatched, never retried.
    assert store.queued_control_operation_count("p1") == 0
    assert state.sent == []


async def test_theater_owned_queue_ignores_theater_policy_unavailability(
    store: Store,
) -> None:
    """QUEUE_FOLLOWUP unavailable/THEATER_POLICY never gates Theater's queue."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    # The frozen Codex contract shape: SEND available, native queue forbidden.
    state.unavailable[RuntimeCapability.QUEUE_FOLLOWUP] = CapabilityUnavailableReason.THEATER_POLICY

    # No native queue API exists on the runtime at all.
    assert [name for name in dir(harness.runtimes["p1"]) if "queue" in name.lower()] == []

    state.native_turn_id = BUSY_TURN  # keep every item queued
    try:
        first, second = [
            await service.queue_followup("p1", caller_id="caller", prompt=f"followup {i}")
            for i in range(2)
        ]
        await drain()
    finally:
        state.native_turn_id = None
    queued = store.queued_control_operations("p1")
    assert [op.queue_sequence for op in queued] == sorted(op.queue_sequence for op in queued)
    assert [op.job_handle for op in queued] == [first.handle, second.handle]

    # FIFO dispatch works exactly as with any other participant: one at a
    # time, and every delivery goes through the ordinary send path.
    outcome = await service.dispatch_queue("p1")
    assert outcome.dispatched == (first.handle,)
    assert store.queued_control_operation_count("p1") == 1
    first_turn = state.native_turn_id
    await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=first_turn),
    )
    state.native_turn_id = None
    await drain()
    assert state.sent == ["followup 0", "followup 1"]
    assert store.queued_control_operation_count("p1") == 0
    assert store.get_job(first.handle).state == JobState.DONE
    assert store.get_job(second.handle).state == JobState.RUNNING


# ---- correction round 1: restart settles jobless orphans --------------------------


async def test_restart_settles_jobless_orphans_making_them_prunable(store: Store) -> None:
    """Hard-crash residue: jobless RESERVED/DISPATCHED operations settle, never linger."""
    harness = await open_harness(store, "p1")
    service = harness.service

    store.reserve_control_operation(
        _make_operation(
            "p1#901:settings_update",
            job_handle=None,
            kind=ControlKind.SETTINGS_UPDATE,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.RESERVED,
            backend_generation=1,
            native_session_id="thread-1",
        )
    )
    store.reserve_control_operation(
        _make_operation(
            "p1#902:interrupt",
            job_handle=None,
            kind=ControlKind.INTERRUPT,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.RESERVED,
            backend_generation=1,
            native_session_id="thread-1",
        )
    )
    store.mark_control_operation_dispatched(
        "p1#902:interrupt", native_session_id="thread-1", updated_at=now()
    )
    assert [
        op.operation_id
        for op in store.control_operations_in_phases(
            "p1", (ControlDeliveryPhase.RESERVED, ControlDeliveryPhase.DISPATCHED)
        )
    ] == ["p1#901:settings_update", "p1#902:interrupt"]

    failed = service.fail_undelivered_followups(["p1"])

    assert failed == []  # no Theater job hangs on a jobless operation
    reserved_op = store.get_control_operation("p1#901:settings_update")
    assert reserved_op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert reserved_op.delivery_result is DeliveryResult.REJECTED
    assert reserved_op.error_code == "daemon_restarted"
    dispatched_op = store.get_control_operation("p1#902:interrupt")
    assert dispatched_op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert dispatched_op.delivery_result is DeliveryResult.UNKNOWN
    assert dispatched_op.error_code == "delivery_unknown"
    # Both rows are settled, so both are prunable — nothing is immortal.
    assert (
        store.control_operations_in_phases(
            "p1", (ControlDeliveryPhase.RESERVED, ControlDeliveryPhase.DISPATCHED)
        )
        == []
    )
    pruned = store.prune_control_operations(older_than=now() + 1.0)
    assert pruned == 2
    assert store.get_control_operation("p1#901:settings_update") is None
    assert store.get_control_operation("p1#902:interrupt") is None


# ---- correction round 3: every pre-transmission crash window closes ----------------


async def test_restart_finishes_orphan_send_job_without_operation(store: Store) -> None:
    """Crash after the job write, before the reservation: finish the orphan."""
    harness = await open_harness(store, "p1")
    job = harness.jobs.create(
        handle="p1#orphan",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="never reserved",
        cwd=None,
    )

    failed = harness.service.fail_undelivered_followups(["p1"])

    assert [j.handle for j in failed] == [job.handle]
    finished = store.get_job(job.handle)
    assert finished.state == JobState.CRASHED
    assert finished.error_code == "daemon_restarted"
    assert "never replayed" in finished.result


async def test_restart_leaves_legacy_jobless_operation_jobs_to_the_observer(
    store: Store,
) -> None:
    """A participant with no runtime keeps its op-less jobs: legacy wiring."""
    harness = Harness(store, {})  # no runtimes: every participant is legacy
    job = harness.jobs.create(
        handle="p1#legacy",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="legacy delivery",
        cwd=None,
    )

    failed = harness.service.fail_undelivered_followups(["p1"])

    assert failed == []
    assert store.get_job(job.handle).state == JobState.RUNNING


async def test_restart_settles_stranded_reserved_rows_on_terminal_jobs(store: Store) -> None:
    """A terminal job's stranded RESERVED row settles — no immortal rows."""
    harness = await open_harness(store, "p1")
    job = harness.jobs.create(
        handle="p1#stranded",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="stranded row",
        cwd=None,
    )
    store.reserve_control_operation(
        _make_operation(
            "p1#stranded:send",
            job_handle=job.handle,
            kind=ControlKind.SEND,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.RESERVED,
        )
    )
    harness.jobs.finish(job.handle, state=JobState.DONE, result="done")

    failed = harness.service.fail_undelivered_followups(["p1"])

    assert failed == []  # the job is already terminal; only the row settles
    op = store.get_control_operation("p1#stranded:send")
    assert op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert op.delivery_result is DeliveryResult.REJECTED
    assert op.error_code == "daemon_restarted"
    assert store.get_job(job.handle).state == JobState.DONE  # untouched


async def test_restart_finishes_running_job_after_operation_settlement(store: Store) -> None:
    """Crash between the operation settlement and the job finish: close it."""
    harness = await open_harness(store, "p1")

    def settled_rejected_job(handle: str, error_code: str | None) -> Job:
        job = harness.jobs.create(
            handle=handle,
            caller_id="caller",
            target_id="p1",
            kind="send",
            prompt="settled then crashed",
            cwd=None,
        )
        store.reserve_control_operation(
            _make_operation(
                f"{handle}:send",
                job_handle=job.handle,
                kind=ControlKind.SEND,
                transport=ControlTransport.NATIVE_RUNTIME,
                phase=ControlDeliveryPhase.RESERVED,
            )
        )
        store.mark_control_operation_dispatched(
            f"{handle}:send", native_session_id="thread-1", updated_at=now()
        )
        store.settle_control_operation(
            f"{handle}:send",
            result=DeliveryResult.REJECTED,
            error_code=error_code,
            error=None if error_code is None else "the native backend refused it",
            updated_at=now(),
        )
        return job

    with_code = settled_rejected_job("p1#settled1", "model_not_allowed")
    without_code = settled_rejected_job("p1#settled2", None)

    failed = harness.service.fail_undelivered_followups(["p1"])

    assert {j.handle for j in failed} == {with_code.handle, without_code.handle}
    first = store.get_job(with_code.handle)
    assert first.state == JobState.CRASHED
    assert first.error_code == "model_not_allowed"  # from the stored row
    assert first.result == "the native backend refused it"
    second = store.get_job(without_code.handle)
    assert second.state == JobState.CRASHED
    assert second.error_code == "send_rejected"  # the documented fallback
    # The settled rows stay settled; nothing new is dispatched or replayed.
    assert state_of(harness, "p1").sent == []


async def test_restart_leaves_dispatched_job_bearing_work_for_exact_reconciliation(
    store: Store,
) -> None:
    """Job-bearing DISPATCHED work is untouched: never replayed, never failed here."""
    harness = await open_harness(store, "p1")
    job = harness.jobs.create(
        handle="p1#inflight",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="in flight",
        cwd=None,
    )
    store.reserve_control_operation(
        _make_operation(
            "p1#inflight:send",
            job_handle=job.handle,
            kind=ControlKind.SEND,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.RESERVED,
            native_session_id="thread-1",
        )
    )
    store.mark_control_operation_dispatched(
        "p1#inflight:send",
        native_session_id="thread-1",
        native_turn_id="turn-inflight",
        updated_at=now(),
    )

    failed = harness.service.fail_undelivered_followups(["p1"])

    assert failed == []
    assert store.get_job(job.handle).state == JobState.RUNNING
    op = store.get_control_operation("p1#inflight:send")
    assert op.delivery_phase is ControlDeliveryPhase.DISPATCHED
    assert op.delivery_result is None  # exactly the pre-crash facts


# ---- correction round 3: replayed interrupt evidence and later queue ---------------


async def test_replayed_interrupt_evidence_does_not_cancel_later_queued_followups(
    store: Store,
) -> None:
    """Queue cancellation runs once; a replay never cancels a later queue."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    await service.send("p1", caller_id="caller", prompt="active work")
    turn = state.native_turn_id
    (queue_a,) = await queue_pending(harness, ["queue A"])

    first = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=turn, terminal=NativeTurnTerminal.INTERRUPTED),
    )
    assert first is not None and first.state == JobState.KILLED
    assert store.get_job(queue_a.handle).state == JobState.KILLED  # A was cancelled

    (queue_b,) = await queue_pending(harness, ["queue B"])

    replay = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=_outcome(state, turn=turn, terminal=NativeTurnTerminal.INTERRUPTED),
    )

    assert replay is not None and replay.state == JobState.KILLED  # finished once already
    assert store.get_job(queue_b.handle).state == JobState.RUNNING  # B stays queued
    (queued_op,) = store.queued_control_operations("p1")
    assert queued_op.job_handle == queue_b.handle


# ---- correction round 3: uncertain settings readback ------------------------------


@pytest.mark.parametrize("queued_at, cancelled", [(120.0, True), (123.5, False), (124.0, False)])
async def test_history_interruption_uses_native_time_not_ingestion_time(
    store: Store, monkeypatch, queued_at: float, cancelled: bool
) -> None:
    harness = await open_harness(store, "p1")
    service = harness.service
    state = state_of(harness, "p1")
    monkeypatch.setattr(service, "_clock", lambda: queued_at)
    try:
        (queued,) = await queue_pending(harness, ["pending"])
        await service.record_terminal_evidence(
            "p1",
            backend_generation=state.backend_generation,
            outcome=NativeTurnOutcome(
                native_session_id=state.native_session_id,
                native_turn_id="old-ui-interruption",
                terminal=NativeTurnTerminal.INTERRUPTED,
                from_history=True,
                completed_at=123.0,
            ),
        )
        expected = JobState.KILLED if cancelled else JobState.RUNNING
        assert store.get_job(queued.handle).state == expected
    finally:
        await service.aclose()


async def test_history_origin_survives_the_persist_finish_crash_window(store: Store) -> None:
    harness = await open_harness(store, "p1")
    service = harness.service
    state = state_of(harness, "p1")
    try:
        old = await service.send("p1", caller_id="caller", prompt="old")
        old_turn = state.native_turn_id
        (queued,) = await queue_pending(harness, ["new intent"])
        store.record_native_terminal_evidence(
            NativeTerminalEvidence(
                participant_id="p1",
                backend_generation=state.backend_generation,
                native_session_id=state.native_session_id,
                native_turn_id=old_turn,
                terminal=NativeTurnTerminal.INTERRUPTED,
                from_history=True,
                completed_at=now() - 60,
                recorded_at=now(),
            )
        )
        await service.record_terminal_evidence(
            "p1",
            backend_generation=state.backend_generation,
            outcome=_outcome(state, turn=old_turn, terminal=NativeTurnTerminal.INTERRUPTED),
        )
        assert store.get_job(old.handle).state == JobState.KILLED
        assert store.get_job(queued.handle).state == JobState.RUNNING
    finally:
        await service.aclose()


async def test_queue_predecessor_advances_with_the_actual_dispatched_turn(store: Store) -> None:
    harness = await open_harness(store, "p1")
    service = harness.service
    state = state_of(harness, "p1")
    try:
        active = await service.send("p1", caller_id="caller", prompt="active")
        first_turn = state.native_turn_id
        first = await service.queue_followup("p1", caller_id="caller", prompt="first")
        second = await service.queue_followup("p1", caller_id="caller", prompt="second")
        assert (
            service.active_job_for_native_turn(
                "p1",
                backend_generation=state.backend_generation,
                native_session_id=state.native_session_id,
                native_turn_id=first_turn,
            ).handle
            == active.handle
        )
        state.native_turn_id = None
        await service.record_terminal_evidence(
            "p1",
            backend_generation=state.backend_generation,
            outcome=_outcome(state, turn=first_turn),
        )
        await wait_until(lambda: len(state.sent) == 2)
        next_turn = state.native_turn_id
        pending = store.queued_control_operations("p1")
        assert len(pending) == 1 and pending[0].native_turn_id is None
        assert json.loads(pending[0].payload)["queue_predecessor_turn"] == next_turn
        state.native_turn_id = None
        await service.record_terminal_evidence(
            "p1",
            backend_generation=state.backend_generation,
            outcome=NativeTurnOutcome(
                native_session_id=state.native_session_id,
                native_turn_id=next_turn,
                terminal=NativeTurnTerminal.INTERRUPTED,
                from_history=True,
            ),
        )
        assert store.get_job(first.handle).state == JobState.KILLED
        assert store.get_job(second.handle).state == JobState.KILLED
        assert len(state.sent) == 2
    finally:
        await service.aclose()


async def test_cancelled_terminal_routing_cannot_consume_an_unmapped_interruption(
    store: Store,
) -> None:
    harness = await open_harness(store, "p1")
    service = harness.service
    state = state_of(harness, "p1")
    try:
        (queued,) = await queue_pending(harness, ["pending"])
        outcome = _outcome(state, turn="ui-turn", terminal=NativeTurnTerminal.INTERRUPTED)
        async with service._lock("p1"):
            routing = asyncio.create_task(
                service.record_terminal_evidence(
                    "p1",
                    backend_generation=state.backend_generation,
                    outcome=outcome,
                )
            )
            await asyncio.sleep(0)
            routing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await routing
            assert (
                store.get_native_terminal_evidence(
                    participant_id="p1",
                    backend_generation=state.backend_generation,
                    native_session_id=state.native_session_id,
                    native_turn_id="ui-turn",
                )
                is None
            )
        await service.record_terminal_evidence(
            "p1",
            backend_generation=state.backend_generation,
            outcome=outcome,
        )
        assert store.get_job(queued.handle).state == JobState.KILLED
    finally:
        await service.aclose()


async def test_settings_readback_failure_reports_uncertain_application(store: Store) -> None:
    """An accepted settings receipt whose readback fails stays uncertain."""

    class ReadbackExplodingRuntime(FakeRuntime):
        """Accepts settings, then fails the effective-value readback."""

        def __init__(self, context) -> None:
            super().__init__(context)
            self.explode_snapshot = False

        async def update_settings(
            self,
            *,
            operation_id: str,
            model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> ControlReceipt:
            receipt = await super().update_settings(
                operation_id=operation_id, model=model, reasoning_effort=reasoning_effort
            )
            self.explode_snapshot = True
            return receipt

        async def snapshot(self):
            if self.explode_snapshot:
                raise RuntimeError("effective-value readback failed")
            return await super().snapshot()

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), ReadbackExplodingRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)

    outcome = await harness.service.update_settings("p1", caller_id="caller", model="m2")

    assert outcome.applied is None  # explicitly uncertain, never success
    assert outcome.model == "m2"
    assert outcome.reasoning_effort is None
    assert outcome.error_code == "delivery_unknown"
    assert outcome.error is not None and "readback" in outcome.error
    # The delivery fact stands; the application is the uncertain part.
    (settings_row,) = operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE)
    assert settings_row["delivery_result"] == str(DeliveryResult.ACCEPTED)
    assert harness.runtimes["p1"].state.settings == {"model": "m2"}


# ---- correction round 3: dispatch-pass exceptions are logged, passes continue -----


async def test_dispatch_pass_exception_is_logged_and_next_pass_runs(store: Store) -> None:
    """A crashed dispatch pass leaves no unretrieved task exception behind."""

    class SnapshotExplodingRuntime(FakeRuntime):
        def __init__(self, context) -> None:
            super().__init__(context)
            self.explode_snapshot = False

        async def snapshot(self):
            if self.explode_snapshot:
                raise RuntimeError("snapshot failed during dispatch")
            return await super().snapshot()

    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    runtime = wrap_runtime(harness.runtimes["p1"], SnapshotExplodingRuntime)
    harness.runtimes["p1"] = runtime

    (queued_job,) = await queue_pending(harness, ["survives the crashed pass"])
    runtime.explode_snapshot = True  # the pass crashes at the idle-check snapshot

    service.schedule_dispatch("p1")
    await drain()

    task = service._dispatch_tasks["p1"]
    assert task.done() and task.exception() is None  # logged, never unretrieved
    assert store.get_job(queued_job.handle).state == JobState.RUNNING  # still queued
    assert store.queued_control_operation_count("p1") == 1
    assert state.sent == []  # nothing dispatched by the crashed pass

    runtime.explode_snapshot = False
    service.schedule_dispatch("p1")
    await drain()

    assert store.queued_control_operation_count("p1") == 0
    assert state.sent == ["survives the crashed pass"]
    assert store.get_job(queued_job.handle).state == JobState.RUNNING  # now active


# ---- native initial-dispatch job reuse (Wave 2C/3 integration) -------------------


def _spawn_job(
    harness: Harness,
    handle: str,
    *,
    target_id: str = "p1",
    caller_id: str = "caller",
    prompt: str = "initial prompt",
    response_format: str | None = None,
) -> Job:
    """The spawn job the native lifecycle creates before initial dispatch."""
    return harness.jobs.create(
        handle=handle,
        caller_id=caller_id,
        target_id=target_id,
        kind="spawn",
        prompt=prompt,
        cwd=None,
        response_format=response_format,
    )


async def test_send_with_spawn_handle_reuses_the_spawn_job(store: Store) -> None:
    """The initial dispatch reuses the spawn job; no second job exists."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    _spawn_job(harness, "p1")

    job = await service.send("p1", caller_id="caller", prompt="initial prompt", job_handle="p1")

    assert job.handle == "p1"  # the spawn job itself — nothing was reminted
    assert job.kind == "spawn"
    assert store.get_job("p1").state == JobState.RUNNING
    (send_op,) = store.control_operations_for_job("p1")
    assert send_op.kind is ControlKind.SEND
    assert send_op.job_handle == "p1"
    assert send_op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert send_op.delivery_result is DeliveryResult.ACCEPTED
    assert state.sent == ["initial prompt"]
    # The reused spawn job is the only running job — and the active one.
    assert [j.handle for j in store.running_jobs_for_target("p1")] == ["p1"]
    assert [j.handle for j in service.active_jobs("p1")] == ["p1"]

    # Exact terminal evidence finishes the existing job, exactly once.
    finished = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=_outcome(state)
    )
    assert finished is not None and finished.handle == "p1"
    assert finished.state == JobState.DONE
    assert [f for f in harness.jobs.finishes if f[0] == "p1"] == [("p1", "done")]


async def test_send_spawn_handle_mismatches_fail_closed(store: Store) -> None:
    """A wrong, terminal, or vanished handle refuses before transmission."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    spawn = _spawn_job(harness, "p1")

    async def refusal(**overrides) -> str:
        kwargs: dict = {
            "caller_id": "caller",
            "prompt": "initial prompt",
            "job_handle": "p1",
        }
        kwargs.update(overrides)
        try:
            await service.send("p1", **kwargs)
            raise AssertionError("a mismatched job handle must refuse")
        except BadRequest as exc:
            return str(exc)

    assert "does not exist" in await refusal(job_handle="p1#missing")
    plain = harness.jobs.create(
        handle="p1#plain",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="initial prompt",
        cwd=None,
    )
    assert "'send'" in await refusal(job_handle="p1#plain")  # not a spawn job
    other_target = _spawn_job(harness, "p2", target_id="p2")
    assert "another participant's" in await refusal(job_handle="p2")
    assert "caller contract" in await refusal(caller_id="someone-else")
    assert "spawn's prompt" in await refusal(prompt="a different prompt")
    assert "response-format contract" in await refusal(response_format="json")

    # Nothing was sent, reserved, or minted; every job is exactly as it was.
    assert state.sent == []
    assert operation_rows(store, "p1", ControlKind.SEND) == []
    assert store.get_job(spawn.handle).state == JobState.RUNNING
    assert store.get_job(plain.handle).state == JobState.RUNNING
    assert store.get_job(other_target.handle).state == JobState.RUNNING

    # A terminal spawn handle refuses too — after it, the job stays as finished.
    harness.jobs.finish(spawn.handle, state=JobState.DONE, result="done")
    assert "running spawn job" in await refusal()
    assert store.get_job(spawn.handle).state == JobState.DONE


async def test_send_spawn_handle_requires_native_runtime(store: Store) -> None:
    """A legacy participant has no native initial dispatch to reuse."""
    harness = Harness(store, {})  # no runtimes: every participant is legacy

    try:
        await harness.service.send(
            "p1", caller_id="caller", prompt="initial prompt", job_handle="p1"
        )
        raise AssertionError("job-handle reuse without a runtime must refuse")
    except BadRequest as exc:
        assert "requires native runtime wiring" in str(exc)
    assert harness.gates_recorder.delivered == []  # nothing reached a pane


async def test_persisted_native_binding_finishes_orphans_before_adoption(store: Store):
    """Restart classifies native-ness from the persisted binding, not the runtime registry."""
    harness = Harness(store, {})  # pre-adoption restart: no live runtimes
    native_spawn = harness.jobs.create(
        handle="p1",
        caller_id="caller",
        target_id="p1",
        kind="spawn",
        prompt="never dispatched",
        cwd=None,
    )
    native_send = harness.jobs.create(
        handle="p1#mid",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="reserved later",
        cwd=None,
    )
    legacy_spawn = harness.jobs.create(
        handle="p2",
        caller_id="caller",
        target_id="p2",
        kind="spawn",
        prompt="legacy delivery",
        cwd=None,
    )
    store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id="p1",
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            created_at=now(),
            updated_at=now(),
        )
    )
    store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id="p2",
            harness="codex",
            wiring=RuntimeWiring.LEGACY,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.INTENDED,
            created_at=now(),
            updated_at=now(),
        )
    )

    failed = harness.service.fail_undelivered_followups(["p1", "p2"])

    assert {job.handle for job in failed} == {native_spawn.handle, native_send.handle}
    orphan = store.get_job(native_spawn.handle)
    assert orphan.state == JobState.CRASHED
    assert orphan.error_code == "daemon_restarted"
    assert "never replayed" in orphan.result
    assert store.get_job(native_send.handle).state == JobState.CRASHED
    legacy = store.get_job(legacy_spawn.handle)
    assert legacy.state == JobState.RUNNING  # the observer owns it


# ---- correction round 2: the initial dispatch is prompt-once ----------------------


async def test_second_reuse_of_the_same_spawn_handle_refuses(store: Store) -> None:
    """One accepted initial dispatch; a second attempt never transmits again."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    _spawn_job(harness, "p1")

    first = await service.send("p1", caller_id="caller", prompt="initial prompt", job_handle="p1")
    assert first.handle == "p1"
    assert store.get_job("p1").state == JobState.RUNNING  # awaiting terminal evidence
    assert state.sent == ["initial prompt"]
    (op,) = store.control_operations_for_job("p1")

    try:
        await service.send("p1", caller_id="caller", prompt="initial prompt", job_handle="p1")
        raise AssertionError("a second initial dispatch of one spawn job must refuse")
    except BadRequest as exc:
        assert "exactly once" in str(exc)

    assert state.sent == ["initial prompt"]  # still exactly one transmission
    assert store.control_operations_for_job("p1") == [op]  # still exactly one operation
    assert store.get_job("p1").state == JobState.RUNNING  # untouched


async def test_reuse_refuses_a_spawn_job_with_a_reserved_operation(store: Store) -> None:
    """A crash-window RESERVED operation blocks a second attempt, any phase."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    _spawn_job(harness, "p1")
    store.reserve_control_operation(
        _make_operation(
            "p1#reserved:send",
            job_handle="p1",
            kind=ControlKind.SEND,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.RESERVED,
            native_session_id=state.native_session_id,
        )
    )

    try:
        await service.send("p1", caller_id="caller", prompt="initial prompt", job_handle="p1")
        raise AssertionError("reuse of a spawn job with a reserved operation must refuse")
    except BadRequest as exc:
        assert "exactly once" in str(exc)

    assert state.sent == []  # nothing was transmitted
    (op,) = store.control_operations_for_job("p1")
    assert op.operation_id == "p1#reserved:send"  # the stranded row is untouched
    assert op.delivery_phase is ControlDeliveryPhase.RESERVED
    assert store.get_job("p1").state == JobState.RUNNING


# ---- disconnected native participants: durable classification, fail closed -------


def disconnected_native_harness(store: Store, *participant_ids: str) -> Harness:
    """A persisted native binding with no live runtime: detached/recovering."""
    harness = Harness(store, {})  # runtime_for is None for every participant
    for pid in participant_ids:
        store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id=pid,
                harness="codex",
                wiring=RuntimeWiring.NATIVE,
                backend_generation=1,
                lifecycle=RuntimeLifecyclePhase.BOUND,
                created_at=now(),
                updated_at=now(),
            )
        )
    return harness


def _no_operations_or_jobs(store: Store, participant_id: str) -> None:
    for kind in (
        ControlKind.SEND,
        ControlKind.STEER,
        ControlKind.QUEUE_FOLLOWUP,
        ControlKind.SETTINGS_UPDATE,
        ControlKind.INTERRUPT,
    ):
        assert operation_rows(store, participant_id, kind) == []
    assert store.running_jobs_for_target(participant_id) == []


async def test_disconnected_native_send_fails_closed_no_legacy_delivery(store: Store):
    """A persisted native binding with no runtime is not legacy: no pane send."""
    harness = disconnected_native_harness(store, "p1")
    recorder = harness.gates_recorder

    try:
        await harness.service.send("p1", caller_id="caller", prompt="no fallback")
        raise AssertionError("a send to a disconnected native must fail closed")
    except StaleTarget as exc:
        assert "natively wired" in str(exc)
        assert "never falls back" in str(exc)

    assert ("p1", "caller", "send") in recorder.authorized  # broad authorization ran
    assert recorder.delivered == []  # no legacy pane delivery
    _no_operations_or_jobs(store, "p1")


async def test_passive_frontend_routes_controls_to_legacy_or_unavailable(store: Store, monkeypatch):
    """A passive frontend never makes a healthy pane control depend on its socket."""
    monkeypatch.setattr(
        "theater.daemon.controls.routing.get_harness",
        lambda name: SimpleNamespace(runtime=OPENCODE_MANIFEST.runtime),
    )
    harness = Harness(store, {})
    store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id="p1",
            harness="opencode",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.ATTACHED,
            created_at=now(),
            updated_at=now(),
        )
    )

    assert harness.service.route_for("p1", RuntimeCapability.SEND).is_legacy
    assert harness.service.route_for("p1", RuntimeCapability.QUEUE_FOLLOWUP).is_legacy
    assert harness.service.route_for("p1", RuntimeCapability.INTERRUPT).is_legacy
    assert harness.service.route_for("p1", RuntimeCapability.STEER).transport is None
    assert harness.service.route_for("p1", RuntimeCapability.SETTINGS_UPDATE).transport is None

    await harness.service.send("p1", caller_id="caller", prompt="legacy first")
    harness.gates_recorder.busy_refusals.add("p1")
    queued = await harness.service.queue_followup("p1", caller_id="caller", prompt="legacy next")
    (queued_operation,) = store.queued_control_operations("p1")
    assert queued_operation.transport is ControlTransport.LEGACY_TMUX

    harness.runtimes["p1"] = make_runtime("p1")
    harness.gates_recorder.busy_refusals.clear()
    outcome = await harness.service.dispatch_queue("p1")
    assert outcome.dispatched == (queued.handle,)
    assert harness.gates_recorder.delivered == [
        ("p1", "legacy first"),
        ("p1", "legacy next"),
    ]

    with pytest.raises(BadRequest, match="unavailable on its selected transport"):
        await harness.service.steer("p1", caller_id="caller", prompt="never native")


async def test_disconnected_native_queue_fails_closed_no_reservation(store: Store):
    """A disconnected native never reserves a queue slot or creates a job."""
    harness = disconnected_native_harness(store, "p1")

    try:
        await harness.service.queue_followup("p1", caller_id="caller", prompt="never queued")
        raise AssertionError("a queue_followup to a disconnected native must fail closed")
    except StaleTarget as exc:
        assert "natively wired" in str(exc)

    assert harness.gates_recorder.delivered == []
    assert store.queued_control_operation_count("p1") == 0
    _no_operations_or_jobs(store, "p1")


async def test_disconnected_native_steer_settings_interrupt_fail_closed(store: Store):
    """Steer, settings, and interrupt refuse with no mutation of any kind."""
    harness = disconnected_native_harness(store, "p1")
    service = harness.service

    try:
        await service.steer("p1", caller_id="caller", prompt="amend")
        raise AssertionError("a steer to a disconnected native must fail closed")
    except StaleTarget as exc:
        assert "never retried" in str(exc)

    try:
        await service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a settings update on a disconnected native must fail closed")
    except StaleTarget:
        pass

    try:
        await service.interrupt("p1", caller_id="caller")
        raise AssertionError("an interrupt of a disconnected native must fail closed")
    except StaleTarget:
        pass

    assert harness.gates_recorder.delivered == []
    _no_operations_or_jobs(store, "p1")


async def test_disconnected_native_interrupt_leaves_the_queue_untouched(store: Store):
    """The interrupt refusal does not even cancel already-queued followups."""
    harness = disconnected_native_harness(store, "p1")
    service = harness.service
    # A followup queued while the runtime was connected; the runtime detached.
    harness.jobs.create(
        handle="p1#q",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="queued before the detach",
        cwd=None,
    )
    store.reserve_control_operation(
        _make_operation(
            "p1#q:queue_followup",
            job_handle="p1#q",
            kind=ControlKind.QUEUE_FOLLOWUP,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.QUEUED,
        )
    )

    try:
        await service.interrupt("p1", caller_id="caller")
        raise AssertionError("an interrupt of a disconnected native must fail closed")
    except StaleTarget:
        pass

    (queued_op,) = store.queued_control_operations("p1")  # nothing was cancelled
    assert queued_op.operation_id == "p1#q:queue_followup"
    assert queued_op.delivery_phase is ControlDeliveryPhase.QUEUED
    assert store.get_job("p1#q").state == JobState.RUNNING


async def test_disconnected_native_queue_dispatch_never_falls_back_to_the_pane(store: Store):
    """A scheduled dispatch pass defers a disconnected native's queue head."""
    harness = disconnected_native_harness(store, "p1")
    service = harness.service
    harness.jobs.create(
        handle="p1#q",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="waits for the runtime",
        cwd=None,
    )
    store.reserve_control_operation(
        _make_operation(
            "p1#q:queue_followup",
            job_handle="p1#q",
            kind=ControlKind.QUEUE_FOLLOWUP,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.QUEUED,
        )
    )

    outcome = await service.dispatch_queue("p1")

    assert outcome.dispatched == () and outcome.failed == ()
    assert outcome.deferred is True
    assert harness.gates_recorder.delivered == []
    assert harness.gates_recorder.busy_checks == []  # the legacy path never ran
    assert store.queued_control_operation_count("p1") == 1
    (queued_op,) = store.queued_control_operations("p1")
    assert queued_op.delivery_phase is ControlDeliveryPhase.QUEUED
    assert store.get_job("p1#q").state == JobState.RUNNING


async def test_disconnected_native_controls_authorize_before_classification(store: Store):
    """An unauthorized caller learns nothing: authorization refuses first."""
    harness = disconnected_native_harness(store, "p1")
    service = harness.service
    harness.gates_recorder.refuse_authorize_for = {"intruder"}

    async def refuses_at_authorization(coro) -> None:
        try:
            await coro
            raise AssertionError("an unauthorized caller must be refused at authorization")
        except NotYourChild:
            pass

    await refuses_at_authorization(service.send("p1", caller_id="intruder", prompt="s"))
    await refuses_at_authorization(service.queue_followup("p1", caller_id="intruder", prompt="q"))
    await refuses_at_authorization(service.steer("p1", caller_id="intruder", prompt="amend"))
    await refuses_at_authorization(service.update_settings("p1", caller_id="intruder", model="m2"))
    await refuses_at_authorization(service.interrupt("p1", caller_id="intruder"))

    # Authorization refused every control before classification: nothing was
    # revealed, delivered, reserved, queued, or finished.
    assert harness.gates_recorder.delivered == []
    _no_operations_or_jobs(store, "p1")
    assert store.queued_control_operations("p1") == []


async def test_legacy_participants_without_bindings_keep_the_legacy_path(store: Store):
    """No native binding: send/queue stay legacy, native-only controls refuse as before."""
    harness = Harness(store, {})

    sent = await harness.service.send("p1", caller_id="caller", prompt="legacy deliver")
    assert sent.kind == "send"
    assert harness.gates_recorder.delivered == [("p1", "legacy deliver")]

    queued = await harness.service.queue_followup("p1", caller_id="caller", prompt="legacy queue")
    assert store.queued_control_operations("p1")[0].job_handle == queued.handle
    (queued_op,) = store.queued_control_operations("p1")
    assert queued_op.transport is ControlTransport.LEGACY_TMUX

    try:
        await harness.service.steer("p1", caller_id="caller", prompt="amend")
        raise AssertionError("steering a legacy participant must refuse")
    except BadRequest as exc:
        assert "requires native runtime" in str(exc)

    try:
        await harness.service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("settings on a legacy participant must refuse")
    except BadRequest as exc:
        assert "fixed at launch" in str(exc)

    try:
        await harness.service.interrupt("p1", caller_id="caller")
        raise AssertionError("interrupting a legacy participant must refuse")
    except BadRequest as exc:
        assert "pane-interrupt path" in str(exc)


async def test_legacy_busy_dispatch_defers_without_mutation(store: Store):
    """A temporarily busy legacy pane defers the head; nothing is delivered or mutated."""
    harness = Harness(store, {})
    service = harness.service
    harness.gates_recorder.busy_refusals = {"p1"}
    first = await service.queue_followup("p1", caller_id="caller", prompt="legacy 0")
    second = await service.queue_followup("p1", caller_id="caller", prompt="legacy 1")
    await drain()  # the scheduled passes must defer, not raise or deliver

    outcome = await service.dispatch_queue("p1")
    assert outcome.deferred is True
    assert outcome.dispatched == ()
    assert outcome.failed == ()

    # No delivery, no dispatch transition, no job finish: the items stay
    # queued and running, exactly as the native busy path leaves them.
    assert harness.gates_recorder.delivered == []
    assert harness.jobs.finishes == []
    # One scheduled pass while the queue grew (deduped) plus the direct one:
    # every pass stopped at the busy check and mutated nothing.
    assert harness.gates_recorder.busy_checks == ["p1", "p1"]
    queued = store.queued_control_operations("p1")
    assert [op.job_handle for op in queued] == [first.handle, second.handle]
    assert all(op.delivery_phase is ControlDeliveryPhase.QUEUED for op in queued)
    assert store.get_job(first.handle).state == JobState.RUNNING
    assert store.get_job(second.handle).state == JobState.RUNNING


async def test_legacy_busy_scheduled_pass_does_not_log_a_crash(store: Store, caplog):
    """The busy refusal is a deferral, not a crashed pass: nothing is logged at ERROR."""
    harness = Harness(store, {})
    harness.gates_recorder.busy_refusals = {"p1"}
    await harness.service.queue_followup("p1", caller_id="caller", prompt="legacy 0")

    with caplog.at_level(logging.DEBUG, logger="theater.daemon.controls"):
        await drain()
        outcome = await harness.service._dispatch_pass_logged("p1")

    assert outcome.deferred is True
    assert outcome.dispatched == () and outcome.failed == ()
    crash_records = [
        record for record in caplog.records if "queue dispatch pass" in record.getMessage()
    ]
    assert not [record for record in crash_records if record.levelno >= logging.ERROR]
    # The pass took the deferral path, visible as the debug deferral record.
    assert any("deferred" in record.getMessage() for record in caplog.records)
    assert store.queued_control_operations("p1")  # the item is still queued


async def test_legacy_busy_head_dispatches_fifo_once_free(store: Store):
    """After the busy condition clears, a later pass dispatches the head once, FIFO."""
    harness = Harness(store, {})
    service = harness.service
    harness.gates_recorder.busy_refusals = {"p1"}
    first = await service.queue_followup("p1", caller_id="caller", prompt="legacy 0")
    second = await service.queue_followup("p1", caller_id="caller", prompt="legacy 1")
    await drain()
    await service.dispatch_queue("p1")  # deferred: still busy

    # The active pane work settles; the busy check no longer refuses.
    harness.gates_recorder.busy_refusals.clear()
    outcome = await service.dispatch_queue("p1")
    assert outcome.dispatched == (first.handle,)
    assert outcome.deferred is False
    assert harness.gates_recorder.delivered == [("p1", "legacy 0")]
    assert [op.job_handle for op in store.queued_control_operations("p1")] == [second.handle]
    assert store.get_job(first.handle).state == JobState.RUNNING  # awaits its evidence

    # The next pass dispatches exactly the next item, in queue order.
    outcome = await service.dispatch_queue("p1")
    assert outcome.dispatched == (second.handle,)
    assert harness.gates_recorder.delivered == [("p1", "legacy 0"), ("p1", "legacy 1")]
    assert store.queued_control_operations("p1") == []
