"""Control service interrupt: queue cancellation, exact-turn requests and interruption evidence."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.control_harness import (
    Harness,
    make_runtime,
    open_harness,
    state_of,
    wrap_runtime,
)
from tests.support.control_service import (
    PENDING_INTERACTION,
    native_outcome,
    queue_pending,
    terminal_evidence_row,
)
from tests.support.fake_runtime import FakeRuntime
from tests.support.waiting import drain
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlReceipt,
    NativeTurnOutcome,
    NativeTurnTerminal,
    SessionOpenMode,
)
from theater.models import (
    JobState,
    now,
)


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
        outcome=native_outcome(
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
        terminal_evidence_row(state, turn, terminal=NativeTurnTerminal.INTERRUPTED)
    )

    finished = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=native_outcome(state, turn=turn, terminal=NativeTurnTerminal.COMPLETED),
    )

    assert finished is not None and finished.state == JobState.KILLED
    assert store.get_job(queued.handle).state == JobState.KILLED


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
        outcome=native_outcome(state, turn=turn, terminal=NativeTurnTerminal.INTERRUPTED),
    )
    assert first is not None and first.state == JobState.KILLED
    assert store.get_job(queue_a.handle).state == JobState.KILLED  # A was cancelled

    (queue_b,) = await queue_pending(harness, ["queue B"])

    replay = await service.record_terminal_evidence(
        "p1",
        backend_generation=state.backend_generation,
        outcome=native_outcome(state, turn=turn, terminal=NativeTurnTerminal.INTERRUPTED),
    )

    assert replay is not None and replay.state == JobState.KILLED  # finished once already
    assert store.get_job(queue_b.handle).state == JobState.RUNNING  # B stays queued
    (queued_op,) = store.queued_control_operations("p1")
    assert queued_op.job_handle == queue_b.handle


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
            outcome=native_outcome(state, turn=old_turn, terminal=NativeTurnTerminal.INTERRUPTED),
        )
        assert store.get_job(old.handle).state == JobState.KILLED
        assert store.get_job(queued.handle).state == JobState.RUNNING
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
        outcome = native_outcome(state, turn="ui-turn", terminal=NativeTurnTerminal.INTERRUPTED)
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
