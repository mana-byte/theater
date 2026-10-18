"""Same-runtime live recovery of a Codex stream: exact session, ambiguous prompts, overflow."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.support.codex_live_recovery import (
    EXACT_RESULT,
    completed_evidence,
    compose_and_spawn,
    disconnect_stream,
    scripted_server,
    spawn_codex,
)
from tests.support.codex_server import (
    summary_turn_completed,
    thread_status_changed,
)
from tests.support.native_lifecycle import teardown_backend
from tests.support.runtime_processes import (
    pid_alive,
)
from tests.support.waiting import wait_until
from theater.daemon.runtime import recovery as recovery_mod
from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.server import Daemon
from theater.harness.builtin.plugins.codex.runtime import CodexRuntime
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeExecutionState,
    RuntimeNotification,
    RuntimeRequestTimeout,
)
from theater.models import JobState


async def _wait_for_runtime_snapshot(
    daemon: Daemon, participant_id: str, predicate, *, what: str, timeout: float = 5.0
):
    """Wait for a real installed runtime snapshot, without driving recovery."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        runtime = daemon.runtime_manager.get(participant_id)
        if runtime is not None:
            snapshot = await runtime.snapshot()
            if predicate(snapshot):
                return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.01)


def _hold_live_recovery(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Return entered/release gates installed before runtime replacement."""
    entered = asyncio.Event()
    release = asyncio.Event()
    recover = recovery_mod.recover_live_runtime

    async def gated_recovery(
        daemon, participant_id: str, backend_generation: int, **kwargs
    ) -> bool:
        entered.set()
        await release.wait()
        return await recover(daemon, participant_id, backend_generation, **kwargs)

    monkeypatch.setattr(recovery_mod, "recover_live_runtime", gated_recovery)
    return entered, release


# ---- automatic disconnect recovery --------------------------------------------


async def test_disconnected_stream_recovers_exact_session_and_completes_the_job(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """Disconnected + backend alive: one fresh initialize, exact resume, exact job."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        assert binding.backend_pid is not None
        backend_pid = binding.backend_pid
        session_id = binding.native_session_id
        assert session_id is not None
        server = scripted_server(io, pid)
        first = d.runtime_manager.get(pid)
        assert isinstance(first, CodexRuntime)

        # The notification stream dies while the verified backend lives on.
        disconnect_stream(io, 0)
        await wait_until(lambda: server.connect_count == 2, what="one automatic reconnect")

        # Exactly one fresh initialize beyond the spawn handshake, and the
        # exact thread/resume of the persisted session id.
        assert len(server.requested("initialize")) == 2
        resumes = server.requested("thread/resume")
        assert resumes[-1] == {
            "threadId": session_id,
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
        # No backend relaunch, no second UI, no prompt replay.
        assert pid_alive(backend_pid), "the live backend is reused, never relaunched"
        assert len(terminal_provider.creations) == 1, "no second UI may be launched"
        assert len(server.requested("turn/start")) == 1, "no prompt is ever replayed"

        # The replacement runtime is a fresh CodexRuntime on the same
        # generation and session, connected and re-registered.
        replacement = d.runtime_manager.get(pid)
        assert isinstance(replacement, CodexRuntime)
        assert replacement is not first
        snapshot = await replacement.snapshot()
        assert snapshot.health.value == "connected"
        assert snapshot.native_session_id == session_id
        assert snapshot.backend_generation == 1
        registration = d.observer.live.registration_for(pid)
        assert registration is not None
        assert registration.live_source is replacement.live_source()

        # Exact terminal evidence on the recovered stream completes the
        # exact spawn job under the original generation/session.
        server.push(completed_evidence())
        await wait_until(
            lambda: (
                d.store.get_job(pid) is not None and d.store.get_job(pid).state == JobState.DONE
            ),
            what="the spawn job completing from exact evidence",
        )
        job = d.store.get_job(pid)
        assert job.result == EXACT_RESULT
        assert (
            d.store.get_native_terminal_evidence(
                participant_id=pid,
                backend_generation=1,
                native_session_id=session_id,
                native_turn_id="turn-1",
            )
            is not None
        )
    finally:
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()


async def test_ambiguous_prompt_start_invalidates_stale_idle_before_fifo_recovery(  # noqa: PLR0915
    theater_home, terminal_provider, monkeypatch
) -> None:
    """A lost turn/start acknowledgement cannot let stale IDLE dispatch FIFO."""
    recovery_entered, recovery_gate = _hold_live_recovery(monkeypatch)
    reconnect_gate = asyncio.Event()
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        assert binding.backend_pid is not None
        session_id = binding.native_session_id
        assert session_id is not None
        backend_pid = binding.backend_pid
        server = scripted_server(io, pid)
        initial = d.runtime_manager.get(pid)
        assert isinstance(initial, CodexRuntime)

        # Establish a genuinely fresh idle baseline after the spawn prompt
        # completed.  The lost-ack control below must not reuse this view.
        server.push(completed_evidence())
        server.push(thread_status_changed("idle", thread_id=session_id))
        await wait_until(
            lambda: (
                d.store.get_job(pid) is not None and d.store.get_job(pid).state == JobState.DONE
            ),
            what="the initial prompt completing",
        )
        await _wait_for_runtime_snapshot(
            d,
            pid,
            lambda snapshot: snapshot.execution_state is RuntimeExecutionState.IDLE,
            what="the initial authoritative idle snapshot",
        )

        # Hold the automatic reconnect while an acknowledgement-lost turn/start leaves only an
        # ambiguous native mutation.
        endpoint = wiring_mod.native_endpoint(pid)
        io.gates[endpoint] = reconnect_gate
        server.fail("turn/start", RuntimeRequestTimeout())
        first = await d.controls.send(pid, caller_id="cli", prompt="unknown first")
        await wait_until(recovery_entered.is_set, what="automatic recovery reaching its gate")
        queued = await d.controls.queue_followup(pid, caller_id="cli", prompt="must stay queued")

        stale_snapshot = await initial.snapshot()
        assert stale_snapshot.health.value == "disconnected"
        assert stale_snapshot.execution_state is RuntimeExecutionState.UNKNOWN
        assert stale_snapshot.native_turn_id is None
        (first_operation,) = d.store.control_operations_for_job(first.handle)
        assert first_operation.execution_barrier is True
        assert d.store.has_execution_barrier(pid)
        assert d.store.queued_control_operation_count(pid) == 1
        assert d.store.get_job(queued.handle).state == JobState.RUNNING
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the task",
            "unknown first",
        ]
        assert terminal_provider.deliveries == [], (
            "an ambiguous native send never falls back to tmux"
        )

        # The automatic monitor reconnects exactly once to the persisted session.
        server.respond(
            "thread/resume",
            {
                "thread": {
                    "id": session_id,
                    "status": {"type": "active"},
                    "turns": [{"id": "turn-ambiguous", "status": "inProgress", "items": []}],
                }
            },
        )
        server.failures.pop("turn/start")
        recovery_gate.set()
        reconnect_gate.set()
        recovered = await _wait_for_runtime_snapshot(
            d,
            pid,
            lambda snapshot: (
                snapshot.execution_state is RuntimeExecutionState.ACTIVE
                and snapshot.native_turn_id == "turn-ambiguous"
            ),
            what="the fresh same-session active recovery view",
        )
        replacement = d.runtime_manager.get(pid)
        assert isinstance(replacement, CodexRuntime)
        assert replacement is not initial
        assert recovered.backend_generation == 1
        assert recovered.native_session_id == session_id
        assert server.connect_count == 2, "one coalesced recovery reconnect"
        assert server.requested("thread/resume")[-1] == {
            "threadId": session_id,
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
        assert pid_alive(backend_pid), "recovery reuses the verified backend"
        assert len(terminal_provider.creations) == 1, "recovery launches no replacement UI"
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the task",
            "unknown first",
        ]
        assert d.store.has_execution_barrier(pid)
        assert d.store.queued_control_operation_count(pid) == 1

        # The recovered live source persists exact terminal evidence before any job mutation.
        server.push(
            summary_turn_completed(
                thread_id=session_id,
                turn_id="turn-ambiguous",
                items_view="summary",
                items=[{"id": "i-ambiguous", "type": "agentMessage", "text": "exact"}],
            )
        )
        await wait_until(
            lambda: (
                d.store.get_native_terminal_evidence(
                    participant_id=pid,
                    backend_generation=1,
                    native_session_id=session_id,
                    native_turn_id="turn-ambiguous",
                )
                is not None
            ),
            what="the recovered exact terminal evidence being persisted",
        )
        evidence = d.store.get_native_terminal_evidence(
            participant_id=pid,
            backend_generation=1,
            native_session_id=session_id,
            native_turn_id="turn-ambiguous",
        )
        assert evidence is not None and evidence.result == "exact"
        assert d.store.get_job(first.handle).state == JobState.RUNNING
        assert d.store.has_execution_barrier(pid)
        assert d.store.queued_control_operation_count(pid) == 1
        assert (
            d.store.control_operation_for_native_turn(
                participant_id=pid,
                backend_generation=1,
                native_session_id=session_id,
                native_turn_id="turn-ambiguous",
            )
            is None
        ), "uncorrelated evidence is never cross-attributed to the unknown prompt"
        assert (
            d.store.get_native_terminal_evidence(
                participant_id=pid,
                backend_generation=2,
                native_session_id=session_id,
                native_turn_id="turn-ambiguous",
            )
            is None
        ), "the recovered evidence stays scoped to its exact generation"

        # Only fresh same-session IDLE can clear the barrier.  Maintenance,
        # not this test, then advances the FIFO head once and only once.
        server.respond("turn/start", {"turn": {"id": "turn-queued", "status": "inProgress"}})
        server.push(thread_status_changed("idle", thread_id=session_id))
        await wait_until(
            lambda: len(server.requested("turn/start")) == 3,
            what="the scheduled FIFO dispatch after authoritative idle",
        )
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the task",
            "unknown first",
            "must stay queued",
        ]
        assert d.store.has_execution_barrier(pid) is False
        assert d.store.get_job(first.handle).state == JobState.RUNNING
        assert d.store.get_job(queued.handle).state == JobState.RUNNING
        assert terminal_provider.deliveries == []
    finally:
        recovery_gate.set()
        reconnect_gate.set()
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()


async def test_cancelled_post_write_prompt_stays_unknown_until_fresh_reconciliation(  # noqa: PLR0915
    theater_home, terminal_provider, monkeypatch
) -> None:
    """A startup-shaped cancellation after turn/start writes cannot reuse IDLE."""
    recovery_entered, recovery_gate = _hold_live_recovery(monkeypatch)
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    reconnect_gate = asyncio.Event()
    write_gate = asyncio.Event()
    cancelled = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        assert binding.backend_pid is not None
        backend_pid = binding.backend_pid
        window_count = len(terminal_provider.creations)
        server = scripted_server(io, pid)
        initial = d.runtime_manager.get(pid)
        assert isinstance(initial, CodexRuntime)

        # The initial Theater prompt completes and establishes a genuinely
        # authoritative idle baseline that the cancelled write must discard.
        server.push(completed_evidence())
        server.push(thread_status_changed("idle", thread_id=session_id))
        await wait_until(
            lambda: (
                d.store.get_job(pid) is not None and d.store.get_job(pid).state == JobState.DONE
            ),
            what="the initial prompt completing before cancellation",
        )
        await _wait_for_runtime_snapshot(
            d,
            pid,
            lambda snapshot: snapshot.execution_state is RuntimeExecutionState.IDLE,
            what="the initial authoritative idle snapshot",
        )

        endpoint = wiring_mod.native_endpoint(pid)
        io.gates[endpoint] = reconnect_gate
        server.request_gates["turn/start"] = write_gate
        write_entered = server.request_entered["turn/start"]
        write_entered.clear()
        cancelled = asyncio.create_task(
            d.controls.send(pid, caller_id="cli", prompt="cancelled after wire write")
        )
        await wait_until(
            write_entered.is_set,
            what="the blocked physical turn/start write",
        )
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        snapshot = await initial.snapshot()
        assert snapshot.health is ConnectionHealth.DISCONNECTED
        assert snapshot.execution_state is RuntimeExecutionState.UNKNOWN
        cancelled_ops = [
            operation
            for operation in d.store.dispatched_control_operations(pid)
            if operation.kind.value == "send"
        ]
        if not cancelled_ops:
            cancelled_ops = [
                operation
                for operation in d.store.control_operations_for_job(
                    next(
                        job.handle
                        for job in d.store.running_jobs_for_target(pid)
                        if job.handle != pid
                    )
                )
                if operation.kind.value == "send"
            ]
        assert len(cancelled_ops) == 1
        operation = cancelled_ops[0]
        assert operation.delivery_result.value == "unknown"
        assert operation.execution_barrier is True

        await wait_until(recovery_entered.is_set, what="automatic recovery reaching its gate")
        queued = await d.controls.queue_followup(pid, caller_id="cli", prompt="must wait")
        await asyncio.sleep(0.08)
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the task",
            "cancelled after wire write",
        ]
        assert d.store.get_job(queued.handle).state == JobState.RUNNING
        assert d.store.has_execution_barrier(pid)
        assert terminal_provider.deliveries == [], (
            "a cancelled native write never falls back to tmux"
        )

        # No helper drives recovery: the monitor reconnects once the blocked
        # transport opens.  Fresh ACTIVE state remains a hard queue boundary.
        server.respond(
            "thread/resume",
            {
                "thread": {
                    "id": session_id,
                    "status": {"type": "active"},
                    "turns": [{"id": "turn-after-cancel", "status": "inProgress", "items": []}],
                }
            },
        )
        recovery_gate.set()
        write_gate.set()
        reconnect_gate.set()
        await _wait_for_runtime_snapshot(
            d,
            pid,
            lambda fresh: (
                fresh.execution_state is RuntimeExecutionState.ACTIVE
                and fresh.native_turn_id == "turn-after-cancel"
                and fresh.native_session_id == session_id
            ),
            what="the fresh exact-session active recovery view",
        )
        assert server.connect_count == 2, (
            "one coalesced reconnect replaces the disconnected runtime"
        )
        assert pid_alive(backend_pid), "recovery reuses the verified backend"
        assert len(terminal_provider.creations) == window_count, (
            "recovery launches no replacement UI"
        )
        assert len(server.requested("turn/start")) == 2
        assert d.store.has_execution_barrier(pid)

        # Only later exact IDLE releases the unresolved execution boundary;
        # daemon-owned maintenance then starts the queued prompt once.
        server.push(thread_status_changed("idle", thread_id=session_id))
        await wait_until(
            lambda: len(server.requested("turn/start")) == 3,
            what="the queued prompt after fresh authoritative idle",
        )
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the task",
            "cancelled after wire write",
            "must wait",
        ]
    finally:
        if cancelled is not None:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
        recovery_gate.set()
        write_gate.set()
        reconnect_gate.set()
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()


async def test_reconnect_recovers_old_theater_turn_beyond_later_native_ui_turns(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """Automatic recovery crosses page 64 and a transient failure, then advances FIFO."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        server = scripted_server(io, pid)
        old_turn = "turn-1"
        queued = await d.controls.queue_followup(pid, caller_id="cli", prompt="after history")
        server.respond(
            "thread/resume",
            {
                "thread": {
                    "id": session_id,
                    "status": {"type": "idle"},
                    "turns": [
                        {"id": "human-2", "status": "completed", "items": []},
                        {"id": "human-3", "status": "completed", "items": []},
                    ],
                }
            },
        )
        failed_once = False

        def history_page(params):
            nonlocal failed_once
            page = int(params.get("cursor", "0"))
            assert params["limit"] == 16
            if page == 32 and not failed_once:
                failed_once = True
                raise RuntimeError("transient history failure")
            if page < 64:
                return {
                    "data": [
                        {"id": f"ui-{page}-{index}", "status": "completed", "items": []}
                        for index in range(16)
                    ],
                    "nextCursor": str(page + 1),
                }
            assert page == 64
            return {
                "data": [
                    {
                        "id": old_turn,
                        "status": "completed",
                        "items": [
                            {
                                "id": "old-final",
                                "type": "agentMessage",
                                "text": "old exact Theater result",
                            }
                        ],
                    }
                ],
                "nextCursor": None,
            }

        server.response_handlers["thread/turns/list"] = history_page
        server.respond("turn/start", {"turn": {"id": "after-history", "status": "inProgress"}})

        # No direct recovery call: the disconnected notification stream wakes
        # the composed manager, which reconnects the same generation/session.
        disconnect_stream(io, 0)
        await wait_until(
            lambda: server.connect_count == 2,
            what="the automatic same-session reconnect",
        )
        await wait_until(
            lambda: (
                d.store.get_job(pid) is not None and d.store.get_job(pid).state == JobState.DONE
            ),
            # 66 sequential mock round trips; a generous bound survives xdist
            # event-loop starvation without weakening the 66-request proof below.
            what="the old exact Theater turn completing through paged recovery",
            timeout=40.0,
        )
        await wait_until(
            lambda: len(server.requested("turn/start")) == 2,
            what="the deferred FIFO advancing without manual dispatch",
        )

        evidence = d.store.get_native_terminal_evidence(
            participant_id=pid,
            backend_generation=1,
            native_session_id=session_id,
            native_turn_id=old_turn,
        )
        assert evidence is not None
        assert evidence.from_history is True
        assert evidence.result == "old exact Theater result"
        assert d.store.get_job(pid).result == "old exact Theater result"
        assert len(server.requested("thread/turns/list")) == 66
        assert server.connect_count == 2, "history retry does not reconnect the runtime"
        assert server.requested("turn/start")[-1]["input"][0]["text"] == "after history"
        assert d.store.get_job(queued.handle).state == JobState.RUNNING
        assert d.store.queued_control_operation_count(pid) == 0
        assert terminal_provider.deliveries == []
    finally:
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()


@pytest.mark.parametrize("case", ["old_ui", "old_finished_job", "missed_current"])
async def test_history_interruption_only_cancels_its_original_followups(
    theater_home, terminal_provider, monkeypatch, case
) -> None:
    """Backfill preserves new intent while a missed current interruption cancels its cohort."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    history_gate = asyncio.Event()
    try:
        pid = await spawn_codex(d)
        session = d.store.get_runtime_binding(pid).native_session_id
        server = scripted_server(io, pid)
        before = None
        if case == "missed_current":
            before = await d.controls.queue_followup(
                pid, caller_id="cli", prompt="before interrupt"
            )
        elif case == "old_finished_job":
            d.jobs.finish(pid, state=JobState.KILLED, error_code="interrupted")

        historical_turn = "old-ui-turn" if case == "old_ui" else "turn-1"
        current_turn = "turn-2" if case == "old_finished_job" else "turn-1"
        active = case != "missed_current"
        server.respond(
            "thread/resume",
            {
                "thread": {
                    "id": session,
                    "status": {"type": "active" if active else "idle"},
                    "turns": [{"id": current_turn, "status": "inProgress", "items": []}]
                    if active
                    else [],
                }
            },
        )
        server.respond(
            "thread/turns/list",
            {
                "data": [{"id": historical_turn, "status": "interrupted", "items": []}],
                "nextCursor": None,
            },
        )
        server.request_gates["thread/turns/list"] = history_gate
        server.respond("turn/start", {"turn": {"id": "new-followup", "status": "inProgress"}})
        disconnect_stream(io, 0)
        await _wait_for_runtime_snapshot(
            d,
            pid,
            lambda snap: (
                server.connect_count == 2
                and snap.health is ConnectionHealth.CONNECTED
                and snap.native_turn_id == (current_turn if active else None)
            ),
            what="the recovered current execution before history is released",
        )
        after = await d.controls.queue_followup(pid, caller_id="cli", prompt="new intent")
        history_gate.set()
        await wait_until(
            lambda: (
                d.store.get_native_terminal_evidence(
                    participant_id=pid,
                    backend_generation=1,
                    native_session_id=session,
                    native_turn_id=historical_turn,
                )
                is not None
            ),
            what="historical interruption persistence",
        )
        assert d.store.get_job(after.handle).state == JobState.RUNNING
        if before is not None:
            await wait_until(
                lambda: len(server.requested("turn/start")) == 2,
                what="new intent progressing after the old interruption",
            )
            assert d.store.get_job(before.handle).state == JobState.KILLED
            assert d.store.get_job(pid).state == JobState.KILLED
            assert server.requested("turn/start")[-1]["input"][0]["text"] == "new intent"
        else:
            assert d.store.queued_control_operation_count(pid) == 1
            assert len(server.requested("turn/start")) == 1
            server.push(
                RuntimeNotification(
                    method="turn/completed",
                    params={
                        "threadId": session,
                        "turn": {"id": current_turn, "status": "interrupted", "items": []},
                    },
                )
            )
            await wait_until(
                lambda: d.store.get_job(after.handle).state == JobState.KILLED,
                what="a current native-UI interruption cancelling the pending followup",
            )
            assert d.store.get_job(after.handle).error_code == "interrupted"
        assert server.connect_count == 2
        assert terminal_provider.deliveries == []
    finally:
        history_gate.set()
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()


# ---- overflow follows the same reconnect path ----------------------------------


@pytest.mark.parametrize("fail_handoff_once", [False, True])
async def test_notification_overflow_drains_evidence_then_recovers(
    theater_home, terminal_provider, monkeypatch, fail_handoff_once
) -> None:
    """An overflow surfaces as a disconnect only after buffered evidence drains."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        server = scripted_server(io, pid)
        endpoint = wiring_mod.native_endpoint(pid)

        runtime = d.runtime_manager.get(pid)
        assert runtime is not None
        source = runtime.live_source()
        read_entered = asyncio.Event()
        sink_calls = 0
        registration = d.observer.live.registration_for(pid)
        assert registration is not None

        async def persist(*args, **kwargs):
            nonlocal sink_calls
            sink_calls += 1
            if fail_handoff_once and sink_calls == 1:
                raise RuntimeError("temporarily unavailable evidence store")
            return await d.controls.record_terminal_evidence(*args, **kwargs)

        async def delayed_read():
            read_entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(source, "read", delayed_read)
        d.observer.live.register(replace(registration, evidence_sink=persist))
        d.observer.live.wake(pid)
        await asyncio.wait_for(read_entered.wait(), timeout=5)

        # Buffer the exact terminal outcome, then overflow the stream: the
        # transport drains the buffered notification first, then raises.
        server.push(completed_evidence())
        io.overflowing.add(endpoint)
        await wait_until(
            lambda: (
                d.store.get_job(pid) is not None and d.store.get_job(pid).state == JobState.DONE
            ),
            what="the buffered exact outcome completing the job",
        )
        await wait_until(lambda: server.connect_count == 2, what="the overflow recovery reconnect")
        assert len(server.requested("turn/start")) == 1, "no prompt is ever replayed"
        assert server.requested("thread/resume")[-1] == {
            "threadId": session_id,
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
        job = d.store.get_job(pid)
        assert job.result == EXACT_RESULT, "no exact terminal outcome is missed"
        assert sink_calls == 1 + int(fail_handoff_once)
    finally:
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()
