"""Control service steer and queued followups: FIFO dispatch, bounds and completion isolation."""

from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select

from tests.support.control_harness import (
    Harness,
    make_runtime,
    open_harness,
    state_of,
    wrap_runtime,
)
from tests.support.control_service import (
    BUSY_TURN,
    PENDING_INTERACTION,
    native_outcome,
    queue_pending,
)
from tests.support.fake_runtime import FakeRuntime
from tests.support.queued_followups import provider_terminal_op
from tests.support.waiting import drain, wait_until
from theater.constants.observation import RESCUE_CONFIRM_SECONDS
from theater.daemon.jobs import JobManager
from theater.daemon.observation.completion import CompletionTracker
from theater.daemon.observer import QuietClock
from theater.daemon.persistence.store import Store
from theater.daemon.schema import touch as touch_table
from theater.harness.contracts.events import EventPath
from theater.harness.contracts.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ControlDeliveryPhase,
    ControlKind,
    ControlReceipt,
    ControlTransport,
    DeliveryResult,
    NativeTurnOutcome,
    NativeTurnTerminal,
    RuntimeCapability,
    SessionOpenMode,
)
from theater.models import (
    Busy,
    JobState,
    StaleTarget,
)


def touch_rows(store: Store, job_handle: str) -> list:
    return list(
        store.conn.execute(
            select(touch_table).where(touch_table.c.job_handle == job_handle)
        ).fetchall()
    )


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
        outcome=native_outcome(state, turn=first_turn),
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
        outcome=native_outcome(state, turn=second_turn),
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
        outcome=native_outcome(state, turn="turn-human"),
    )
    assert human is None  # no Theater job owns the human turn
    assert store.get_job(queued.handle).state == JobState.RUNNING

    # Completing the active job writes touches for it — and none for the
    # pending followup, which has no accumulator.
    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=native_outcome(state, turn=active_turn),
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
        outcome=native_outcome(state, turn=first_turn),
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
        outcome=native_outcome(state, turn=second_turn),
    )
    assert finished is not None and finished.handle == queued.handle
    assert finished.state == JobState.DONE
    assert len(touch_rows(store, queued.handle)) == 1


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
        outcome=native_outcome(state, turn=first_turn),
    )
    state.native_turn_id = None
    await drain()
    assert state.sent == ["followup 0", "followup 1"]
    assert store.queued_control_operation_count("p1") == 0
    assert store.get_job(first.handle).state == JobState.DONE
    assert store.get_job(second.handle).state == JobState.RUNNING


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
            outcome=native_outcome(state, turn=first_turn),
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


PROMPT_OBSERVER = SimpleNamespace(
    screen_reading=lambda capture: ScreenReading(
        kind=ScreenKind.PROMPT, confidence=ScreenConfidence.LOW
    )
)


def _delivered_plus_queued(registry):
    """Job A delivered (send settled accepted) and followup B still queued."""
    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    jobs.create(handle="a", caller_id="caller", target_id=p.id, kind="send", prompt="a prompt")
    jobs.create(handle="b", caller_id="caller", target_id=p.id, kind="send", prompt="b prompt")
    with registry.store.write_unit() as unit:
        registry.store.reserve_control_operation(
            provider_terminal_op(
                "op-a",
                p.id,
                kind=ControlKind.SEND,
                phase=ControlDeliveryPhase.SETTLED,
                job_handle="a",
                result=DeliveryResult.ACCEPTED,
            ),
            connection=unit.connection,
        )
        registry.store.reserve_control_operation(
            provider_terminal_op(
                "op-b",
                p.id,
                kind=ControlKind.QUEUE_FOLLOWUP,
                phase=ControlDeliveryPhase.QUEUED,
                job_handle="b",
            ),
            connection=unit.connection,
        )
    tracker = CompletionTracker(registry.store, registry, jobs_fn=lambda: jobs)
    return p, jobs, tracker


async def _capture(_pid):
    return "$ "


async def _rescue(tracker, pid, clock):
    """Arm on one idle capture, then confirm on a later one."""
    for at in (0.0, RESCUE_CONFIRM_SECONDS):
        await tracker.rescue_jobs(
            pid, PROMPT_OBSERVER, clock, rescue_timeout=60.0, capture_fn=_capture, now=at
        )


def _op_phase(registry, operation_id):
    op = registry.store.get_control_operation(operation_id)
    assert op is not None
    return op.delivery_phase


async def test_rescue_finishes_only_the_delivered_job(registry):
    """A quiet bare-prompt rescue must finish A and leave queued B untouched."""
    p, jobs, tracker = _delivered_plus_queued(registry)
    clock = QuietClock()
    clock.last_text = "the last thing it said"

    await _rescue(tracker, p.id, clock)

    a = jobs.get("a")
    assert a.state == JobState.DONE
    assert a.error_code == "turn_end_unseen"
    assert a.result == "the last thing it said"
    b = jobs.get("b")
    assert b.state == JobState.RUNNING
    assert b.result is None
    assert _op_phase(registry, "op-b") == ControlDeliveryPhase.QUEUED


def test_answer_turn_never_touches_a_queued_followup(registry):
    """After A's turn ends, later human turns must not answer or crash B."""
    p, jobs, tracker = _delivered_plus_queued(registry)

    tracker.answer_turn(p.id, "a's answer", heard=["a prompt"])
    a = jobs.get("a")
    assert a.state == JobState.DONE
    assert a.result == "a's answer"

    # Human turns that answer someone else: neither soaked up nor fatal for B.
    tracker.answer_turn(p.id, "human chatter", heard=["unrelated"])
    tracker.answer_turn(p.id, "more chatter", heard=["also unrelated"])
    tracker.answer_turn(p.id, "even more", heard=["still unrelated"])
    b = jobs.get("b")
    assert b.state == JobState.RUNNING
    assert b.result is None
    assert b.error_code is None
    assert _op_phase(registry, "op-b") == ControlDeliveryPhase.QUEUED


async def test_plain_job_without_control_operation_still_completes(registry):
    """A plain running job (no control operation) keeps the old behaviour."""
    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    jobs.create(handle="c", caller_id="caller", target_id=p.id, kind="send", prompt="c prompt")
    tracker = CompletionTracker(registry.store, registry, jobs_fn=lambda: jobs)

    tracker.answer_turn(p.id, "c's answer", heard=["c prompt"])
    assert jobs.get("c").result == "c's answer"

    jobs.create(handle="d", caller_id="caller", target_id=p.id, kind="send", prompt="d prompt")
    clock = QuietClock()
    clock.last_text = "rescued text"
    await _rescue(tracker, p.id, clock)
    d = jobs.get("d")
    assert d.state == JobState.DONE
    assert d.error_code == "turn_end_unseen"
