"""Control service restart recovery: undelivered work is settled and never replayed."""

from __future__ import annotations

from tests.support.control_harness import (
    Harness,
    make_operation,
    open_harness,
    state_of,
)
from tests.support.control_service import queue_pending
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import (
    Job,
    JobState,
    now,
)


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
        make_operation(
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


async def test_restart_settles_jobless_orphans_making_them_prunable(store: Store) -> None:
    """Hard-crash residue: jobless RESERVED/DISPATCHED operations settle, never linger."""
    harness = await open_harness(store, "p1")
    service = harness.service

    store.reserve_control_operation(
        make_operation(
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
        make_operation(
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
        make_operation(
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
            make_operation(
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
        make_operation(
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
