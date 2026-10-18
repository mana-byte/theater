"""Control service receipts: a receipt settles only its own operation."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.control_harness import (
    Harness,
    make_operation,
    make_runtime,
    state_of,
    wrap_runtime,
)
from tests.support.control_service import native_outcome, operation_rows
from tests.support.fake_runtime import FakeRuntime
from tests.support.waiting import wait_until
from theater.daemon.controls import recovery as control_recovery_module
from theater.daemon.controls import service as control_service_module
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
    JobState,
    now,
)


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
    monkeypatch.setattr(control_recovery_module, "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS", 0.01)
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
            outcome=native_outcome(state, turn=turn),
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
        make_operation(
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
        outcome=native_outcome(state, turn=original_turn),
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
