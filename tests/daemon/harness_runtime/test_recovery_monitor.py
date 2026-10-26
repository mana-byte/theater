"""Recovery monitor ownership: isolation, teardown and shutdown races, generation replacement."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import replace

from tests.support.codex_live_recovery import (
    EXACT_RESULT,
    CodexLiveHarness,
    RoutingCodexIO,
    completed_evidence,
    compose_and_spawn,
    disconnect_stream,
    scripted_server,
    spawn_codex,
)
from tests.support.codex_server import (
    RequestFailure,
)
from tests.support.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from tests.support.native_lifecycle import teardown_backend
from tests.support.runtime_processes import (
    await_reaped,
    fake_create,
    pid_alive,
)
from tests.support.waiting import wait_until
from theater.daemon.harness_runtime import manager as manager_mod
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.runtime import recovery as recovery_mod
from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.server import Daemon
from theater.harness.builtin.plugins.codex.runtime import CodexRuntime
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeLifecyclePhase,
    SessionOpenMode,
)
from theater.models import JobState

# ---- stale persisted generation never recovers ---------------------------------


async def test_stale_persisted_generation_cannot_reconnect(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """A monitor whose persisted generation was replaced recovers nothing."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        server = scripted_server(io, pid)

        # A replacement generation now owns the persisted binding.
        d.store.upsert_runtime_binding(replace(binding, backend_generation=2))
        disconnect_stream(io, 0)

        def _route_disconnected() -> bool:
            route = d.runtime_manager.cached_native_facts(
                pid,
                backend_generation=binding.backend_generation,
                native_session_id=binding.native_session_id,
            )
            return route is not None and route.health is ConnectionHealth.DISCONNECTED

        # The monitor observes the disconnect; it must still never reconnect,
        # because the persisted generation was replaced.
        await wait_until(_route_disconnected, what="the monitor to observe the disconnect")
        assert server.connect_count == 1, "a stale generation never reconnects"
        assert d.runtime_manager.get(pid) is not None, "the live runtime is left untouched"

        # Restore the exact binding for the teardown cleanup.
        d.store.upsert_runtime_binding(binding)
    finally:
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()


# ---- one blocked reconnect never blocks another participant ---------------------


async def test_blocked_reconnect_for_one_participant_does_not_block_another(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """A blocked recovery/open leaves the other participant fully responsive."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid1 = pid2 = None
    gate = asyncio.Event()
    try:
        pid1 = await spawn_codex(d)
        pid2 = await spawn_codex(d)
        server1 = scripted_server(io, pid1)
        server2 = scripted_server(io, pid2)
        initial2 = d.runtime_manager.get(pid2)
        assert isinstance(initial2, CodexRuntime)
        p2_turn_starts = list(server2.requested("turn/start"))

        # Hold pid2's reconnect connect open; then disconnect both streams.
        endpoint2 = wiring_mod.native_endpoint(pid2)
        p2_connect_entered = io.connect_entered.setdefault(endpoint2, asyncio.Event())
        io.gates[endpoint2] = gate
        disconnect_stream(io, 0)
        disconnect_stream(io, 1)

        await wait_until(
            p2_connect_entered.is_set,
            what="p2's reconnect entering the blocked connect",
        )
        # ``reconnect`` installs this exact replacement before its open_session reaches the gated
        # connect.
        runtime2 = d.runtime_manager.get(pid2)
        assert isinstance(runtime2, CodexRuntime)
        assert runtime2 is not initial2
        snapshot2 = await runtime2.snapshot()
        assert snapshot2.health in (ConnectionHealth.UNOPENED, ConnectionHealth.DISCONNECTED)
        assert snapshot2.execution_state is RuntimeExecutionState.UNKNOWN
        assert server2.requested("turn/start") == p2_turn_starts
        assert server2.connect_count == 1

        await wait_until(lambda: server1.connect_count == 2, what="p1's unblocked recovery")
        runtime1 = d.runtime_manager.get(pid1)
        assert runtime1 is not None
        snapshot1 = await runtime1.snapshot()
        assert snapshot1.health.value == "connected", "p1 recovered while p2 is blocked"

        # Runtime/control lookups stay responsive for both participants.
        assert d.runtime_manager.get(pid1) is not None
        assert d.runtime_manager.get(pid2) is runtime2
        assert server2.connect_count == 1

        gate.set()
        await wait_until(lambda: server2.connect_count == 2, what="p2's recovery after the gate")
        runtime2 = d.runtime_manager.get(pid2)
        assert runtime2 is not None
        snapshot2 = await runtime2.snapshot()
        assert snapshot2.health.value == "connected", "p2 recovers once unblocked"
    finally:
        gate.set()
        for pid in (pid1, pid2):
            if pid is not None:
                await teardown_backend(d, pid)
        await d.aclose()
        assert d.runtime_manager._monitors == {}
        assert d.controls.owned_tasks == ()


# ---- close/teardown and daemon shutdown own the monitor ------------------------


async def test_disconnect_then_teardown_performs_no_post_teardown_reconnect(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """Teardown cancels the monitor: no reconnect, and the backend terminates."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None and binding.backend_pid is not None
        server = scripted_server(io, pid)

        disconnect_stream(io, 0)
        await d.runtime_manager.teardown(pid, backend_generation=1)
        await wait_until(
            lambda: d.runtime_manager._monitors == {}, what="the owned monitor to be cancelled"
        )
        assert server.connect_count == 1, "no reconnect may follow the teardown"
        assert d.runtime_manager._monitors == {}, "the owned monitor is cancelled"
        await await_reaped(binding.backend_pid)
    finally:
        if pid is not None:
            with contextlib.suppress(Exception):
                await d.runtime_manager.teardown(pid, backend_generation=1)
        await d.aclose()


async def test_disconnect_then_daemon_close_performs_no_post_close_reconnect(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """Daemon shutdown cancels the monitors and never terminates the backend."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None and binding.backend_pid is not None
        server = scripted_server(io, pid)

        disconnect_stream(io, 0)
        await d.aclose()
        await wait_until(
            lambda: d.runtime_manager._monitors == {}, what="every owned monitor to be cancelled"
        )
        assert server.connect_count == 1, "no reconnect may follow daemon shutdown"
        assert d.runtime_manager._monitors == {}, "every owned monitor is cancelled"
        assert pid_alive(binding.backend_pid), "the backend and its UI survive shutdown"
    finally:
        if pid is not None:
            with contextlib.suppress(Exception):
                # Reap the surviving backend the way the next daemon's
                # teardown would: verified identity, explicit generation.
                await d.runtime_manager.teardown(pid, backend_generation=1)


async def test_shutdown_revokes_recovery_blocked_in_session_open(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """A cancellation-resistant open cannot re-register after recovery stops."""
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_DRAIN_TIMEOUT_SECONDS", 0.01)
    open_entered = asyncio.Event()
    open_cancelled = asyncio.Event()
    release_open = asyncio.Event()
    original_open = CodexRuntime.open_session

    async def stubborn_open(runtime, *, mode, native_session_id=None):
        if mode is SessionOpenMode.RECONNECT:
            open_entered.set()
            try:
                await release_open.wait()
            except asyncio.CancelledError:
                open_cancelled.set()
                await release_open.wait()
        return await original_open(runtime, mode=mode, native_session_id=native_session_id)

    monkeypatch.setattr(CodexRuntime, "open_session", stubborn_open)
    io, d = await compose_and_spawn(terminal_provider, monkeypatch, poll=0.01, retry=0.01)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None and binding.backend_pid is not None
        original_runtime = d.runtime_manager.get(pid)
        registrations: list[str] = []
        route_changes: list[str] = []

        def record_registration(daemon, recovered_binding, runtime, manifest) -> None:
            del daemon, runtime, manifest
            registrations.append(recovered_binding.participant_id)

        monkeypatch.setattr(recovery_mod, "_register_live", record_registration)
        d.runtime_manager.set_route_change_callback(route_changes.append)

        disconnect_stream(io, 0)
        await asyncio.wait_for(open_entered.wait(), 5.0)
        candidate = d.runtime_manager.get(pid)
        assert candidate is not None and candidate is not original_runtime
        route_changes.clear()

        stopping = asyncio.create_task(d.runtime_manager.stop_recovery())
        await asyncio.wait_for(open_cancelled.wait(), 5.0)
        await asyncio.wait_for(stopping, 5.0)
        assert registrations == route_changes == []

        release_open.set()
        await wait_until(
            lambda: not d.runtime_manager._draining_monitors,
            what="the revoked recovery callback finishing",
        )
        assert registrations == route_changes == []
        assert (await candidate.snapshot()).health is ConnectionHealth.DISCONNECTED
        assert pid_alive(binding.backend_pid), "revocation disconnects but never kills the backend"
    finally:
        release_open.set()
        if pid is not None:
            with contextlib.suppress(Exception):
                await d.runtime_manager.teardown(pid, backend_generation=1)
        await d.aclose()


# ---- post-await ownership boundaries -----------------------------------------


def _gen2_runtime(participant_id: str) -> FakeRuntime:
    """The replacement generation's runtime: a fresh, contract-faithful fake."""
    state = FakeRuntimeState(participant_id=participant_id, backend_generation=2)
    state.native_session_id = "gen-2-session"
    context = RuntimeContext(
        participant_id=participant_id,
        cwd="/tmp",
        io=FakeRuntimeIO(state),
        backend_generation=2,
        endpoint="unix:///tmp/thtr-gen2.sock",
    )
    return FakeRuntime(context)


def _register_gen2(daemon: Daemon, participant_id: str, runtime: FakeRuntime) -> None:
    """Register the replacement generation's live source, as its lifecycle does."""
    channel = CodexLiveHarness(RoutingCodexIO())._manifest().channel
    daemon.observer.live.register(
        LiveRegistration(
            participant_id=participant_id,
            live_source=runtime.live_source(),
            channel=channel,
            backend_generation=2,
            native_session_id="gen-2-session",
            evidence_sink=daemon.controls.record_terminal_evidence,
            active_job_for_turn=daemon.controls.active_job_for_native_turn,
        )
    )


async def test_generation_replacement_during_open_never_registers_stale_recovery(  # noqa: PLR0915
    theater_home, terminal_provider, monkeypatch
) -> None:
    """The gated open race: a stale completion returns False, registers nothing."""
    # A quiet monitor: this regression drives the recovery function directly
    # so the race is deterministic; the monitor path is proven elsewhere.
    io, d = await compose_and_spawn(terminal_provider, monkeypatch, poll=3600.0, retry=0.05)
    pid = None
    candidate = None
    recovery_task = None
    gate = asyncio.Event()
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        first = d.runtime_manager.get(pid)
        assert first is not None

        # Hold the recovery candidate's open_session at its connect.
        endpoint = wiring_mod.native_endpoint(pid)
        io.gates[endpoint] = gate
        connect_entered = io.connect_entered.setdefault(endpoint, asyncio.Event())
        recovery_task = asyncio.create_task(recovery_mod.recover_live_runtime(d, pid, 1))
        await wait_until(
            lambda: d.runtime_manager.get(pid) is not first, what="the candidate installing"
        )
        candidate = d.runtime_manager.get(pid)
        await wait_until(
            connect_entered.is_set, what="the candidate's open reaching the gated connect"
        )
        assert not recovery_task.done(), "the recovery is parked at the gated open"

        await d.runtime_manager.teardown(pid, backend_generation=1)
        d.store.upsert_runtime_binding(
            replace(binding, backend_generation=2, native_session_id="gen-2-session")
        )
        gen2 = _gen2_runtime(pid)
        await d.runtime_manager.get_or_create(
            pid,
            backend_generation=2,
            create=fake_create(gen2),
        )
        _register_gen2(d, pid, gen2)
        registration_before = d.observer.live.registration_for(pid)
        assert registration_before is not None
        assert registration_before.backend_generation == 2

        gate.set()
        recovered = await asyncio.wait_for(recovery_task, 10.0)

        assert recovered is False, "a stale completion must return False"
        assert d.runtime_manager.get(pid) is gen2, "generation 2 remains current"
        registration = d.observer.live.registration_for(pid)
        assert registration is registration_before
        assert registration.live_source is gen2.live_source()
        assert registration.backend_generation == 2, "no cross-generation registration"
        assert registration.native_session_id == "gen-2-session"
    finally:
        if recovery_task is not None and not recovery_task.done():
            gate.set()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(recovery_task, 10.0)
        if candidate is not None:
            with contextlib.suppress(Exception):
                # The stale candidate is no longer owned by the manager; close
                # its own connection only, never any successor's.
                await candidate.aclose()
        if pid is not None:
            with contextlib.suppress(Exception):
                await d.runtime_manager.teardown(pid, backend_generation=2)
            with contextlib.suppress(Exception):
                await d.runtime_manager.teardown(pid, backend_generation=1)
        await d.aclose()


async def test_failed_open_after_installation_retries_and_eventually_registers(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """A failed session open is discarded in place and retried, bounded."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        server = scripted_server(io, pid)
        assert server.connect_count == 1

        # Every re-adoptation of the exact session fails until the test
        # clears the failure: the candidate installs, then open fails.
        server.fail("thread/resume", RequestFailure("scripted resume refused"))
        disconnect_stream(io, 0)

        # The monitor retries: at least two failed candidates connect
        # beyond the spawn handshake before the failure is cleared.
        await wait_until(lambda: server.connect_count >= 3, what="the bounded retry cadence")
        server.failures.pop("thread/resume", None)

        def _recovered() -> bool:
            registration = d.observer.live.registration_for(pid)
            runtime = d.runtime_manager.get(pid)
            return (
                registration is not None
                and runtime is not None
                and registration.live_source is runtime.live_source()
            )

        await wait_until(_recovered, what="the eventual registration")
        runtime = d.runtime_manager.get(pid)
        assert isinstance(runtime, CodexRuntime)
        snapshot = await runtime.snapshot()
        assert snapshot.health.value == "connected"
        assert snapshot.native_session_id == session_id
        assert snapshot.backend_generation == 1
        # No relaunch, no second UI, no prompt replay across all attempts.
        assert pid_alive(binding.backend_pid)
        assert len(terminal_provider.creations) == 1
        assert len(server.requested("turn/start")) == 1

        # The eventually-registered runtime completes the exact job.
        server.push(completed_evidence())

        def _job_done() -> bool:
            job = d.store.get_job(pid)
            return job is not None and job.state == JobState.DONE

        await wait_until(_job_done, what="the job completing after the retried recovery")
        assert d.store.get_job(pid).result == EXACT_RESULT
    finally:
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()


async def test_registration_failure_is_retryable_and_never_closes_a_successor(  # noqa: PLR0915
    theater_home, terminal_provider, monkeypatch
) -> None:
    """A failed registration discards the candidate in place, never a successor."""
    # A wide retry gap so the discarded-in-place state is sampled
    # deterministically between the failed attempt and the next one.
    io, d = await compose_and_spawn(terminal_provider, monkeypatch, retry=0.5)
    pid = None
    fail_registration = False
    retry_connect_gate = asyncio.Event()
    failed_candidate: CodexRuntime | None = None
    failed_candidate_was_current = False
    failed_candidate_discarded = asyncio.Event()
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        endpoint = wiring_mod.native_endpoint(pid)

        # Recovery-owned cleanup sentinels: recording spies, because a raise
        # would only be absorbed by the recovery's fail-open handler.
        close_calls: list[str] = []
        unregister_calls: list[str] = []
        real_close = d.runtime_manager.close
        real_unregister = d.observer.live.unregister

        async def close_spy(participant_id, **kwargs):
            close_calls.append(participant_id)
            return await real_close(participant_id, **kwargs)

        def unregister_spy(participant_id):
            unregister_calls.append(participant_id)
            return real_unregister(participant_id)

        monkeypatch.setattr(d.runtime_manager, "close", close_spy)
        monkeypatch.setattr(d.observer.live, "unregister", unregister_spy)

        # Observe the exact candidate's disconnect, not a later manager lookup that can already be a
        # retry candidate.
        real_aclose = CodexRuntime.aclose

        async def aclose_spy(runtime):
            nonlocal failed_candidate_was_current
            await real_aclose(runtime)
            if runtime is failed_candidate:
                failed_candidate_was_current = d.runtime_manager.get(pid) is runtime
                failed_candidate_discarded.set()

        monkeypatch.setattr(CodexRuntime, "aclose", aclose_spy)

        # Registration fails while the flag is set; the monitor's recovery
        # must then discard the candidate in place and retry.
        registration_failures = 0
        real_register = d.observer.live.register

        def register_spy(registration):
            nonlocal failed_candidate, registration_failures
            if fail_registration:
                registration_failures += 1
                failed_candidate = d.runtime_manager.get(pid)
                # A retry cannot replace the exact discarded candidate before the assertion below.
                io.gates[endpoint] = retry_connect_gate
                raise RuntimeError("scripted registration failure")
            return real_register(registration)

        fail_registration = True
        monkeypatch.setattr(d.observer.live, "register", register_spy)

        disconnect_stream(io, 0)
        await wait_until(lambda: registration_failures >= 1, what="the failed registration")
        await asyncio.wait_for(failed_candidate_discarded.wait(), timeout=5.0)

        # The candidate that failed registration is still the manager's current runtime — discarded
        # in place, fail-closed, and now reading DISCONNECTED so the monitor retries.
        assert isinstance(failed_candidate, CodexRuntime)
        assert failed_candidate_was_current, "the failed candidate is not removed from the manager"
        snapshot = await failed_candidate.snapshot()
        assert snapshot.health.value == "disconnected", (
            "a failed candidate never stays connected-but-unusable"
        )

        fail_registration = False
        retry_connect_gate.set()

        def _registered_current() -> bool:
            registration = d.observer.live.registration_for(pid)
            runtime = d.runtime_manager.get(pid)
            return (
                registration is not None
                and runtime is not None
                and registration.backend_generation == 1
                and registration.native_session_id == session_id
                and registration.live_source is runtime.live_source()
            )

        await wait_until(
            _registered_current, what="the eventual registration after the scripted failure"
        )

        # Recovery never closed the manager's runtime and never
        # unregistered anyone's live wiring, through failure and success.
        assert close_calls == [], "recovery must never close the manager's runtime"
        assert unregister_calls == [], "recovery must never unregister live wiring"
    finally:
        # An assertion above must not leave the monitor repeatedly retrying a
        # deliberately failing registration while teardown waits for it.
        fail_registration = False
        retry_connect_gate.set()
        if pid is not None:
            await teardown_backend(d, pid)
        await d.aclose()


async def test_a_backend_that_exits_mid_turn_settles_its_open_job_as_crashed(
    theater_home, terminal_provider, monkeypatch
) -> None:
    """A turn in flight when the backend process dies can never write its
    terminal marker: the job must surface as crashed (backend_gone), never
    as success, and the monitor must not retry a dead process forever."""
    io, d = await compose_and_spawn(terminal_provider, monkeypatch)
    pid = None
    try:
        pid = await spawn_codex(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None and binding.backend_pid is not None
        server = scripted_server(io, pid)
        # The turn is genuinely in flight: the prompt was accepted and no
        # terminal evidence has arrived.
        assert server.requested("turn/start")
        assert d.store.get_job(pid).state == JobState.RUNNING

        # The backend process exits mid-turn, and its stream dies with it.
        os.kill(binding.backend_pid, signal.SIGKILL)
        await await_reaped(binding.backend_pid)
        disconnect_stream(io, 0)

        await wait_until(
            lambda: d.store.get_job(pid).state == JobState.CRASHED,
            what="the mid-flight job settling as crashed",
        )
        job = d.store.get_job(pid)
        assert job.error_code == "backend_gone"
        assert job.result, "the crash names what happened and what to do"
        failed = d.store.get_runtime_binding(pid)
        assert failed is not None
        assert failed.lifecycle is RuntimeLifecyclePhase.FAILED
        # Recovery settled the death instead of churning reconnects at a
        # process that can never answer: only the spawn handshake connected.
        assert server.connect_count == 1, "no reconnect may be attempted"
    finally:
        if pid is not None:
            backend = d.runtime_manager.backend(pid)
            if backend is not None and backend.alive():
                await teardown_backend(d, pid)
        await d.aclose()
