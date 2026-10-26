"""Control service refusals: busy and unknown targets name the operation and a remedy."""

from __future__ import annotations

from tests.support.control_harness import (
    make_operation,
    open_harness,
    state_of,
)
from tests.support.control_service import BUSY_TURN, PENDING_INTERACTION, queue_pending
from tests.support.waiting import drain, wait_until
from theater.daemon.controls import service as control_service_module
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    RuntimeExecutionState,
)
from theater.models import (
    AwaitingDecision,
    Busy,
    JobState,
    now,
)


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
    except Busy as exc:
        assert "omit if_busy to queue" in exc.details["next"]

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


async def test_busy_refusal_names_the_runtime_before_the_undrainable_queue(
    store: Store,
) -> None:
    """A queued-and-disconnected participant cannot drain: name the runtime."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    await queue_pending(harness, ["first"])
    state.health = ConnectionHealth.DISCONNECTED
    try:
        await harness.service.send("p1", caller_id="caller", prompt="jumper")
        raise AssertionError("a queued-and-disconnected target must refuse the send")
    except Busy as exc:
        assert "no live native connection" in str(exc)
        assert "queued followup" not in str(exc)
    finally:
        state.health = ConnectionHealth.CONNECTED
    assert state.sent == []


async def test_busy_refusal_names_the_runtime_before_the_active_turn(store: Store) -> None:
    """An active turn on a disconnected runtime cannot be interrupted either."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    state.native_turn_id = "turn-cut-off"
    state.health = ConnectionHealth.DISCONNECTED
    try:
        await harness.service.send("p1", caller_id="caller", prompt="nope")
        raise AssertionError("a disconnected runtime must refuse the send")
    except Busy as exc:
        assert "no live native connection" in str(exc)
        assert "native turn" not in str(exc)
    finally:
        state.health = ConnectionHealth.CONNECTED
        state.native_turn_id = None
    assert state.sent == []


async def test_busy_refusal_names_the_barrier_before_running_jobs(store: Store) -> None:
    """An execution barrier outranks other running jobs in the refusal."""
    harness = await open_harness(store, "p1")
    job = harness.jobs.create(
        handle="p1#resv",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="dispatched, unresolved",
        cwd=None,
    )
    harness.store.reserve_control_operation(
        make_operation(
            "p1#resv:send",
            job_handle=job.handle,
            kind=ControlKind.SEND,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.DISPATCHED,
            native_session_id="stale-session",
        )
    )
    harness.store.set_control_execution_barrier("p1#resv:send", active=True, updated_at=now())
    try:
        await harness.service.send("p1", caller_id="caller", prompt="second")
        raise AssertionError("an execution barrier must refuse the send")
    except Busy as exc:
        assert "unresolved native prompt delivery" in str(exc)
        assert "running send job" not in str(exc)


async def test_settings_refusal_also_names_the_runtime_before_the_queue(
    store: Store,
) -> None:
    """The idle-only settings update names liveliness first too."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    await queue_pending(harness, ["first"])
    state.health = ConnectionHealth.DISCONNECTED
    try:
        await harness.service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a queued-and-disconnected target must refuse settings")
    except Busy as exc:
        assert "no live native connection" in str(exc)
        assert "not delivering a settings update" in str(exc)
        assert "queued followup" not in str(exc)
    finally:
        state.health = ConnectionHealth.CONNECTED
    assert state.sent == []


async def test_settings_refusal_on_a_queued_target_names_wait_and_retry(store: Store) -> None:
    """Queued followups refuse settings with a wait-and-retry remedy."""
    harness = await open_harness(store, "p1")
    await queue_pending(harness, ["first"])
    try:
        await harness.service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a queued target must refuse settings")
    except Busy as exc:
        text = str(exc)
        assert "settings update cannot jump ahead" in text
        assert "retry once idle" in text
        assert "ordinary send" not in text
        assert "queue another followup" not in text
        assert "not injecting a new prompt" not in text


async def test_settings_refusal_on_an_active_turn_names_wait_and_retry(store: Store) -> None:
    """Settings keep their own remedy; no interrupt, no followup queueing."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    state.native_turn_id = "turn-live"
    try:
        await harness.service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("an active turn must refuse settings")
    except Busy as exc:
        text = str(exc)
        assert "not delivering a settings update" in text
        assert "settings require an idle participant" in text
        assert "wait for the turn to end, then retry" in text
        assert "Call interrupt" not in text
        assert "queue a followup" not in text
    finally:
        state.native_turn_id = None
    assert state.sent == []


async def test_settings_refusal_on_a_running_job_names_wait_and_retry(store: Store) -> None:
    """A running send job refuses settings with its own remedy."""
    harness = await open_harness(store, "p1")
    harness.jobs.create(
        handle="p1#busy",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="in flight",
        cwd=None,
    )
    try:
        await harness.service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a running send job must refuse settings")
    except Busy as exc:
        text = str(exc)
        assert "running send job" in text
        assert "wait for it to finish, then retry" in text
        assert "not injecting a new prompt" not in text


async def test_send_refusal_on_an_active_turn_names_the_send_remedy(store: Store) -> None:
    """Any sender can omit if_busy to queue behind the active turn."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    state.native_turn_id = "turn-live"
    try:
        await harness.service.send("p1", caller_id="caller", prompt="too soon")
        raise AssertionError("an active turn must refuse the send")
    except Busy as exc:
        text = str(exc)
        assert "not injecting a new prompt" in text
        assert "send again with if_busy omitted" in text
        assert "omit if_busy to queue" in exc.details["next"]
    finally:
        state.native_turn_id = None
    assert state.sent == []


SENDING = "not injecting a new prompt"


UPDATING = "not delivering a settings update"


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
