"""The daemon control service: durable control state machine over fake runtimes.

Every behavior the runtime-wiring plan requires of the Wave 2C control
service, driven against ``tests/rig/fake_runtime.py`` — no native backend,
no tmux, no client surfaces:

* operation reservation before transmission and receipt transitions, native
  and legacy;
* ordinary send: pane/policy preflight gates via injected callbacks,
  authoritative idle check, per-participant serialization, reject known busy
  and queued-ahead, the accepted (guarded, not atomic) native-UI race;
* steering: exact current job and expected turn, stale/no-turn refusal,
  no synthetic job;
* queued followups: awaitable job immediately, persisted FIFO, bound,
  one-at-a-time dispatch after an authoritative idle check, authorization
  revalidation at dispatch, temporary-busy deferral, restart failure
  without replay;
* settings: idle-only, capability/allowlist gates, supplied fields only;
* interrupt: queue cancellation under the dispatch lock, exact-turn
  interruption, native-UI-initiated interruption hook;
* exact job-to-turn mapping, evidence-before-finish, crash recovery,
  duplicate evidence finishing once;
* queued jobs excluded from results, touches, and the active-job selectors;
* participant B completing while participant A's runtime I/O blocks.

Queue tests keep the participant busy while items are queued: an idle
participant dispatches its queue head on the next scheduling opportunity,
so an idle harness would race the tests instead of sitting still.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.daemon.controls import ControlGates, ControlService
from theater.daemon.jobs import JobManager
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.persistence.store import Store
from theater.daemon.schema import control_operations as control_operations_table
from theater.daemon.schema import touch as touch_table
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
    RuntimeLifecyclePhase,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.models import (
    AwaitingDecision,
    BadRequest,
    Busy,
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
        self.preflights: list[str] = []
        self.busy_checks: list[str] = []
        self.prompt_checks: list[str] = []
        self.settings_checks: list[tuple[str | None, str | None]] = []
        self.delivered: list[tuple[str, str]] = []
        self.refuse_dispatch_callers: set[str] = set()
        self.refuse_preflight_for: set[str] = set()

    def gates(self) -> ControlGates:
        async def send_preflight(participant_id: str) -> None:
            self.preflights.append(participant_id)
            if participant_id in self.refuse_preflight_for:
                raise StaleTarget(f"pane of {participant_id!r} no longer exists")

        async def legacy_busy_check(participant_id: str) -> None:
            self.busy_checks.append(participant_id)

        async def legacy_deliver(participant_id: str, prompt: str) -> None:
            self.delivered.append((participant_id, prompt))

        def authorize(participant_id: str, caller_id: str, action: str) -> None:
            self.authorized.append((participant_id, caller_id, action))
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
            send_preflight=send_preflight,
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
    """Queue followups that stay pending: busy while queued, then idle.

    An idle participant auto-dispatches its head on the next scheduling
    opportunity; a busy one leaves everything queued, which is what the
    queue tests need to observe FIFO, bounds, and cancellation.
    """
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
    # Its binding survives in the control-operation table, so a buggy or
    # racing backend reporting the same turn for a second send must not
    # rebind it to a second Theater job.
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
        send_preflight=_noop_preflight,
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
    """Persisted evidence A wins over a conflicting duplicate B after a crash.

    The evidence commit and the job finish are separate writes; a crash
    between them leaves evidence A stored and the job still running. When a
    conflicting duplicate B arrives, the job must finish from the persisted
    first write — never from the incoming duplicate.
    """
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
    # The forged job is already terminal, so the busy gate ignores it — but
    # its operation still poisons the exact-turn mapping, which is the bug
    # state the newer accepted send must refuse to settle into.
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
    """QUEUE_FOLLOWUP unavailable/THEATER_POLICY never gates Theater's queue.

    Codex intentionally reports the QUEUE_FOLLOWUP capability unavailable
    with THEATER_POLICY: it marks forbidden native thread/queue use. The
    followup queue is Theater-owned, so queueing and FIFO dispatch must work
    with SEND available, and no native queue method exists or is invoked.
    """
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

    # Forge the residue of a crash mid-control: a settings operation that was
    # reserved but never transmitted, and an interrupt whose transmission
    # began but whose acknowledgement never arrived.
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
    """Crash after the job write, before the reservation: finish the orphan.

    A native participant's running send job with no control operation is
    never a legacy job — nothing exact can ever reconcile it, so restart
    finishes it crashed instead of leaving it immortal.
    """
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
    """Crash between the operation settlement and the job finish: close it.

    The stored refusal facts decide the job's terminal result and error
    code; a missing error code falls back to send_rejected.
    """
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
    """Queue cancellation runs once; a replay never cancels a later queue.

    The first INTERRUPTED evidence cancels the queue of that moment. A
    followup queued afterwards is new intent; replaying the same evidence
    must leave it queued — the cancellation is an irreversible side effect
    that belongs to the first evidence insertion only.
    """
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


async def test_settings_readback_failure_reports_uncertain_application(store: Store) -> None:
    """An accepted settings receipt whose readback fails stays uncertain.

    The operation row keeps its accepted delivery fact, but the outcome
    reports ``applied=None`` with an explicit uncertainty — never success,
    and never a raised exception out of the control.
    """

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
    """A crashed dispatch pass leaves no unretrieved task exception behind.

    The pass is not retried blindly; the queue head survives it, and the
    next scheduling opportunity runs another pass that dispatches normally.
    """

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
    """Restart classifies native-ness from the persisted binding, not the runtime registry.

    Reconciliation runs before the runtime manager adopts any backend, so
    ``runtime_for`` is still ``None``; a persisted NATIVE binding must still
    mark the participant native, and its op-less crash-window jobs (spawn or
    send) finish daemon_restarted. A persisted LEGACY binding — like no
    binding at all — keeps op-less jobs with the observer.
    """
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
