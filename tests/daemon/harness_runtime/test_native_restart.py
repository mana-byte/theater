"""Native-runtime daemon shutdown, restart reconnect and startup resume races."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.support.fake_runtime import (
    FakeRuntime,
    FakeRuntimeIO,
    FakeRuntimeState,
)
from tests.support.native_lifecycle import (
    NativeFixtureHarness,
    RecordingRuntime,
    RoutingIO,
    native_daemon,
    spawn_native,
    spawn_request,
    teardown_backend,
)
from tests.support.runtime_processes import (
    await_reaped,
    fake_create,
    pid_alive,
)
from tests.support.waiting import wait_until
from theater.daemon.harness_runtime import manager as manager_mod
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
)
from theater.daemon.runtime import recovery
from theater.daemon.server import Daemon
from theater.harness import HARNESSES
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeManifest,
    RuntimeWiring,
    SessionOpenMode,
)

pytestmark = pytest.mark.usefixtures("native_harness_binary")


class _ScriptedResumeRuntime(RecordingRuntime):
    """A scripted exact-session re-adoption.

    A RECONNECT open parks on `resume_gate` while set, then fails while `resume_fails` is set.
    """

    def __init__(
        self, context: RuntimeContext, events: list, harness: _ScriptedResumeHarness
    ) -> None:
        super().__init__(context, events)
        self._scripted_harness = harness

    async def open_session(self, *, mode, native_session_id=None):
        if mode is not SessionOpenMode.RECONNECT:
            return await super().open_session(mode=mode, native_session_id=native_session_id)
        if self._scripted_harness.resume_gate is not None:
            self._events.append(("open_parked", native_session_id))
            await self._scripted_harness.resume_gate.wait()
        if self._scripted_harness.resume_fails:
            self._events.append(("open_failed", mode, native_session_id))
            raise ConnectionError("scripted transient resume failure")
        return await super().open_session(mode=mode, native_session_id=native_session_id)


class _ScriptedResumeHarness(NativeFixtureHarness):
    """The lifecycle harness whose restart re-adoption the test scripts.

    While `resume_fails` is set, runtimes fail their RECONNECT open until the test clears it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.resume_fails = True
        self.resume_gate: asyncio.Event | None = None

    def _manifest(self) -> RuntimeManifest:
        base = super()._manifest()
        harness = self

        def factory(context: RuntimeContext):
            if harness.resume_fails:
                return _ScriptedResumeRuntime(context, harness.events, harness)
            return base.factory(context)

        return replace(base, factory=factory)


def _replacement_runtime(participant_id: str) -> FakeRuntime:
    """The replacement generation's runtime: a fresh, contract-faithful fake."""
    state = FakeRuntimeState(participant_id=participant_id, backend_generation=2)
    state.native_session_id = "gen-2-session"
    state.health = ConnectionHealth.CONNECTED
    context = RuntimeContext(
        participant_id=participant_id,
        cwd="/tmp",
        io=FakeRuntimeIO(state),
        backend_generation=2,
        endpoint="unix:///tmp/thtr-gen2.sock",
    )
    return FakeRuntime(context)


async def _restart_daemon(io, harness, terminal_provider) -> Daemon:
    """A fresh daemon over the same home, composed like every restart test."""
    d = Daemon(harnesses={})
    HARNESSES[harness.name] = harness
    d.runtime_io = io
    d.spawner.runtime_io = io
    harness.store = d.store
    await d.start()
    return d


# ---- shutdown: disconnect only ------------------------------------------------


async def test_shutdown_disconnects_clients_and_leaves_the_backend_alive(
    theater_home, terminal_provider, native_rig
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = await spawn_native(d, spawn_request(prompt="long turn"))
    binding = d.store.get_runtime_binding(p.id)
    terminal = d.store.terminal_bindings.get(p.id)
    assert terminal is not None
    runtime = d.runtime_manager.get(p.id)
    assert runtime.state.connected

    await d.aclose()

    assert runtime.state.connected is False, "the runtime client is disconnected"
    assert pid_alive(binding.backend_pid), "a healthy backend survives shutdown"
    assert terminal.terminal_id in terminal_provider.terminal_ids, "the UI survives shutdown"
    # Leave no process behind the test.
    await teardown_backend(d, p.id)


# ---- restart reconciliation ----------------------------------------------------


async def test_restart_reconnects_the_exact_session_without_a_second_ui(
    theater_home, terminal_provider, native_rig
):
    d1 = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = await spawn_native(d1, spawn_request(prompt=""))  # promptless: nothing queued
    binding = d1.store.get_runtime_binding(p.id)
    windows_before = len(terminal_provider.creations)
    await d1.aclose()
    assert pid_alive(binding.backend_pid)

    # A fresh daemon over the same home reconciles before observation.
    d2 = Daemon(harnesses={})
    HARNESSES[native_rig.harness.name] = native_rig.harness
    d2.runtime_io = native_rig.io
    d2.spawner.runtime_io = native_rig.io
    native_rig.harness.store = d2.store
    followups_seen: list[list[str]] = []
    evidence_seen: list[list[str]] = []
    real_followups = d2.controls.fail_undelivered_followups
    real_evidence = d2.controls.finish_jobs_from_pending_evidence

    def spy_followups(participant_ids, **kwargs):
        followups_seen.append(list(participant_ids))
        return real_followups(participant_ids, **kwargs)

    def spy_evidence(participant_ids):
        evidence_seen.append(list(participant_ids))
        return real_evidence(participant_ids)

    d2.controls.fail_undelivered_followups = spy_followups
    d2.controls.finish_jobs_from_pending_evidence = spy_evidence
    try:
        await d2.start()
        assert followups_seen == [[p.id]], "undelivered work is failed before adoption"
        assert evidence_seen == [[p.id]], "stored evidence is consumed once reconnected"

        reconnected = d2.runtime_manager.get(p.id)
        assert isinstance(reconnected, RecordingRuntime)
        assert reconnected.state.native_session_id == binding.native_session_id
        adopted = d2.store.get_runtime_binding(p.id)
        assert adopted.native_session_id == binding.native_session_id
        assert adopted.backend_pid == binding.backend_pid
        refreshed = d2.registry.get(p.id)
        assert refreshed.session_id == binding.native_session_id
        assert len(terminal_provider.creations) == windows_before, "no second UI is launched"
    finally:
        await teardown_backend(d2, p.id)
        await d2.aclose()


async def test_restart_reconnect_registers_live_wiring_before_evidence_is_consumed(
    theater_home, terminal_provider, native_rig
):
    d1 = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = await spawn_native(d1, spawn_request(prompt=""))  # promptless: nothing queued
    binding = d1.store.get_runtime_binding(p.id)
    await d1.aclose()
    assert pid_alive(binding.backend_pid)

    d2 = Daemon(harnesses={})
    HARNESSES[native_rig.harness.name] = native_rig.harness
    d2.runtime_io = native_rig.io
    d2.spawner.runtime_io = native_rig.io
    native_rig.harness.store = d2.store
    # The evidence sink must see the live wiring already registered.
    seen_at_evidence: dict[str, object] = {}
    real_evidence = d2.controls.finish_jobs_from_pending_evidence

    def spy_evidence(participant_ids):
        for pid in participant_ids:
            seen_at_evidence[pid] = d2.observer.live.registration_for(pid)
        return real_evidence(participant_ids)

    d2.controls.finish_jobs_from_pending_evidence = spy_evidence
    try:
        await d2.start()
        registration = d2.observer.live.registration_for(p.id)
        assert registration is not None, "reconnect registers after the exact open"
        runtime = d2.runtime_manager.get(p.id)
        assert registration.live_source is runtime.live_source()
        assert registration.channel is native_rig.harness.runtime.channel
        assert registration.backend_generation == binding.backend_generation
        assert registration.native_session_id == binding.native_session_id
        assert registration.evidence_sink == d2.controls.record_terminal_evidence
        assert registration.active_job_for_turn == d2.controls.active_job_for_native_turn
        assert seen_at_evidence == {p.id: registration}, (
            "stored terminal evidence reconciles through the live registration"
        )

        await recovery.teardown_participant_runtime(d2, p.id, caller_id="cli")
        assert d2.observer.live.registration_for(p.id) is None
        await await_reaped(binding.backend_pid)
    finally:
        if pid_alive(binding.backend_pid):
            with contextlib.suppress(Exception):
                await teardown_backend(d2, p.id)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(binding.backend_pid, signal.SIGKILL)
        await d2.aclose()


async def test_restart_fails_unverified_backend_and_marks_the_binding_failed(
    theater_home, terminal_provider, native_rig
):
    d1 = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = await spawn_native(d1, spawn_request(prompt=""))
    binding = d1.store.get_runtime_binding(p.id)
    # A recycled pid / changed start identity: adoption must fail closed.
    d1.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=binding.participant_id,
            harness=binding.harness,
            wiring=binding.wiring,
            backend_generation=binding.backend_generation,
            lifecycle=binding.lifecycle,
            endpoint=binding.endpoint,
            backend_pid=binding.backend_pid,
            backend_started_at=binding.backend_started_at + 555.0,
            native_session_id=binding.native_session_id,
            protocol=binding.protocol,
            protocol_version=binding.protocol_version,
            native_version=binding.native_version,
            compatibility_policy=binding.compatibility_policy,
            launch_policy=binding.launch_policy,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
        )
    )
    await d1.aclose()

    d2 = Daemon(harnesses={})
    HARNESSES[native_rig.harness.name] = native_rig.harness
    d2.runtime_io = native_rig.io
    d2.spawner.runtime_io = native_rig.io
    native_rig.harness.store = d2.store
    try:
        await d2.start()
        failed = d2.store.get_runtime_binding(p.id)
        assert failed is not None
        assert failed.lifecycle is RuntimeLifecyclePhase.FAILED
        assert d2.runtime_manager.get(p.id) is None
        kinds = {row["kind"] for row in d2.store.bus_tail(limit=50)}
        assert "runtime.backend_gone" in kinds
    finally:
        await d2.aclose()
        # The real backend was never adopted by d2; d1's manager still owns it.
        await teardown_backend(d1, p.id)


async def test_pre_identity_crash_residue_is_diagnosed_and_never_adopted(theater_home):
    d = Daemon(harnesses={})
    try:
        # Residue of a crash between intent persistence and backend start.
        participant = d.registry.create_spawned(
            harness="native-fixture", cwd="/tmp", has_prompt=True
        )
        d.store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id=participant.id,
                harness="native-fixture",
                wiring=RuntimeWiring.NATIVE,
                backend_generation=1,
                lifecycle=RuntimeLifecyclePhase.INTENDED,
            )
        )
        await d._reconcile()
        assert d.runtime_manager.get(participant.id) is None
        rows = [row for row in d.store.bus_tail(limit=50) if row["kind"] == "runtime.orphan"]
        assert rows, "orphan diagnostics are exposed"
        assert "not launch a second UI" in rows[0]["payload"]["reason"]
        assert d.store.get_runtime_binding(participant.id) is not None, "kept for diagnostics"
    finally:
        await d.aclose()


async def test_live_backend_without_persisted_session_is_owned_but_not_attached(
    theater_home, terminal_provider, native_rig
):
    d1 = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = await spawn_native(d1, spawn_request(prompt=""))
    binding = d1.store.get_runtime_binding(p.id)
    # A crash after the backend started but before identity was persisted.
    d1.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=binding.participant_id,
            harness=binding.harness,
            wiring=binding.wiring,
            backend_generation=binding.backend_generation,
            lifecycle=binding.lifecycle,
            endpoint=binding.endpoint,
            backend_pid=binding.backend_pid,
            backend_started_at=binding.backend_started_at,
            launch_policy=binding.launch_policy,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
        )
    )
    await d1.aclose()
    assert pid_alive(binding.backend_pid)

    d2 = Daemon(harnesses={})
    HARNESSES[native_rig.harness.name] = native_rig.harness
    d2.runtime_io = native_rig.io
    d2.spawner.runtime_io = native_rig.io
    native_rig.harness.store = d2.store
    try:
        await d2.start()
        # Ownership adopted (a later kill can terminate it), session not.
        assert d2.runtime_manager.get(p.id) is None, "no runtime is created"
        rows = [row for row in d2.store.bus_tail(limit=50) if row["kind"] == "runtime.orphan"]
        assert rows and "no native session identity" in rows[0]["payload"]["reason"]
        assert d2.runtime_manager.backend(p.id) is not None, "backend ownership adopted"

        # Ownership means termination: the teardown path can still stop it.
        await recovery.teardown_participant_runtime(d2, p.id, caller_id="cli")
        await await_reaped(binding.backend_pid)
        assert d2.store.get_runtime_binding(p.id) is None
    finally:
        await d2.aclose()


# ---- startup recovery: a failed open/registration is fail-closed and retryable ---


async def test_startup_resume_failure_is_fail_closed_and_monitor_recovers(  # noqa: PLR0915
    theater_home, terminal_provider, monkeypatch
):
    """A failed startup re-adoption must never read CONNECTED forever.

    The candidate is discarded in place; the monitor retries the exact session without a
    relaunch, second UI or replayed prompt.
    """
    io = RoutingIO()
    harness = _ScriptedResumeHarness()
    d1 = await native_daemon(io, harness, terminal_provider)
    d2 = None
    p = None
    backend_pid = None
    try:
        p = await spawn_native(d1, spawn_request(prompt=""))  # promptless: nothing queued
        binding = d1.store.get_runtime_binding(p.id)
        assert binding is not None
        assert binding.native_session_id is not None
        backend_pid = binding.backend_pid
        assert backend_pid is not None
        windows_before = len(terminal_provider.creations)
        await d1.aclose()
        assert pid_alive(backend_pid)

        monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.05)
        monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.05)
        d2 = await _restart_daemon(io, harness, terminal_provider)

        # The startup reconciliation failed its exact-session open: the
        # installed candidate is unusable and must read DISCONNECTED, so the
        # manager's monitor retries instead of a connected-but-unusable
        # runtime suppressing it.
        candidate = d2.runtime_manager.get(p.id)
        assert candidate is not None, "the failed candidate stays owned for the retry"
        snapshot = await candidate.snapshot()
        assert snapshot.health is ConnectionHealth.DISCONNECTED, (
            "a candidate with no native session must not read CONNECTED"
        )
        assert d2.observer.live.registration_for(p.id) is None
        assert pid_alive(backend_pid), "the backend is never relaunched"
        assert len(terminal_provider.creations) == windows_before, "no second UI is launched"
        kinds = {row["kind"] for row in d2.store.bus_tail(limit=50)}
        assert "runtime.orphan" in kinds, "the failure stays diagnosed"

        # While the transient failure persists, the bounded cadence retries
        # the exact session without ever registering.
        def _failed_opens() -> int:
            return sum(1 for event in harness.events if event[0] == "open_failed")

        first_failures = _failed_opens()
        await asyncio.sleep(0.25)
        assert _failed_opens() > first_failures, "the monitor retries on its bounded cadence"
        assert d2.observer.live.registration_for(p.id) is None
        current = d2.runtime_manager.get(p.id)
        assert current is not None
        snapshot = await current.snapshot()
        assert snapshot.health is ConnectionHealth.DISCONNECTED, (
            "nothing registers without a session"
        )

        # The transient failure clears: the monitor — never a manual call —
        # reconnects the exact persisted session and registers live wiring.
        harness.resume_fails = False
        await wait_until(
            lambda: d2.observer.live.registration_for(p.id) is not None,
            what="the monitor-driven live registration",
        )
        runtime = d2.runtime_manager.get(p.id)
        assert runtime is not candidate, "recovery replaces the failed candidate"
        snapshot = await runtime.snapshot()
        assert snapshot.health is ConnectionHealth.CONNECTED
        assert snapshot.native_session_id == binding.native_session_id
        assert snapshot.backend_generation == binding.backend_generation
        registration = d2.observer.live.registration_for(p.id)
        assert registration is not None
        assert registration.live_source is runtime.live_source()
        assert registration.native_session_id == binding.native_session_id
        assert registration.backend_generation == binding.backend_generation
        assert not any(event[0] == "send" for event in harness.events), "no prompt is ever replayed"
        assert pid_alive(backend_pid), "the same verified backend is reused"
        assert len(terminal_provider.creations) == windows_before
    finally:
        if d2 is not None:
            with contextlib.suppress(Exception):
                await recovery.teardown_participant_runtime(d2, p.id, caller_id="cli")
            await d2.aclose()
        if backend_pid is not None and pid_alive(backend_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(backend_pid, signal.SIGKILL)
            await await_reaped(backend_pid)


async def test_startup_registration_failure_is_fail_closed_and_monitor_registers(  # noqa: PLR0915
    theater_home, terminal_provider, monkeypatch
):
    """A failed startup live registration must not leave a connected dead wire.

    The candidate is discarded so the monitor retries and registers the exact session.
    """
    io = RoutingIO()
    harness = _ScriptedResumeHarness()
    harness.resume_fails = False  # the open succeeds; only the registration fails
    real_register = recovery._register_live
    # Registration fails until the test has inspected the failed candidate, so a
    # fast monitor retry cannot replace it first.
    failures = {"n": 0, "heal": False}

    def flaky_register(daemon, binding, runtime, manifest):
        if not failures["heal"]:
            failures["n"] += 1
            raise RuntimeError("scripted live registration failure")
        return real_register(daemon, binding, runtime, manifest)

    monkeypatch.setattr(recovery, "_register_live", flaky_register)
    d1 = await native_daemon(io, harness, terminal_provider)
    d2 = None
    p = None
    backend_pid = None
    try:
        p = await spawn_native(d1, spawn_request(prompt=""))
        binding = d1.store.get_runtime_binding(p.id)
        assert binding is not None
        assert binding.native_session_id is not None
        backend_pid = binding.backend_pid
        windows_before = len(terminal_provider.creations)
        await d1.aclose()
        assert pid_alive(backend_pid)

        monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.05)
        monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.05)
        d2 = await _restart_daemon(io, harness, terminal_provider)

        # The startup registration failed: no live wiring exists and the
        # candidate must read DISCONNECTED, so the monitor retries.
        candidate = d2.runtime_manager.get(p.id)
        assert candidate is not None
        snapshot = await candidate.snapshot()
        assert snapshot.health is ConnectionHealth.DISCONNECTED, (
            "a candidate with no live registration must not read CONNECTED"
        )
        assert d2.observer.live.registration_for(p.id) is None
        assert failures["n"] >= 1, "the scripted registration failure happened"
        assert d2.store.get_runtime_binding(p.id) is not None, "the binding is kept"
        failures["heal"] = True

        # The monitor — never a manual call — retries and registers the
        # exact runtime/session once the transient failure has passed.
        await wait_until(
            lambda: d2.observer.live.registration_for(p.id) is not None,
            what="the monitor-driven live registration",
        )
        runtime = d2.runtime_manager.get(p.id)
        assert runtime is not candidate, "recovery replaces the failed candidate"
        snapshot = await runtime.snapshot()
        assert snapshot.health is ConnectionHealth.CONNECTED
        assert snapshot.native_session_id == binding.native_session_id
        registration = d2.observer.live.registration_for(p.id)
        assert registration is not None
        assert registration.live_source is runtime.live_source()
        assert registration.native_session_id == binding.native_session_id
        assert not any(event[0] == "send" for event in harness.events), "no prompt is ever replayed"
        assert pid_alive(backend_pid), "the same verified backend is reused"
        assert len(terminal_provider.creations) == windows_before, "no second UI is launched"
    finally:
        if d2 is not None:
            with contextlib.suppress(Exception):
                await recovery.teardown_participant_runtime(d2, p.id, caller_id="cli")
            await d2.aclose()
        if backend_pid is not None and pid_alive(backend_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(backend_pid, signal.SIGKILL)
            await await_reaped(backend_pid)


@asynccontextmanager
async def _startup_parked_behind_a_successor(terminal_provider, monkeypatch):
    """Restart a daemon whose startup re-adoption parks on a gate, then replace its generation.

    While the generation-1 open waits, generation 2 replaces the persisted binding and the
    manager's runtime and registers its own live source, as a replacement lifecycle does.
    """
    io = RoutingIO()
    harness = _ScriptedResumeHarness()
    d1 = await native_daemon(io, harness, terminal_provider)
    d2 = None
    try:
        p = await spawn_native(d1, spawn_request(prompt=""))
        binding = d1.store.get_runtime_binding(p.id)
        assert binding is not None
        assert binding.native_session_id is not None
        assert binding.backend_pid is not None
        await d1.aclose()

        # A quiet monitor: the race is driven through the real startup
        # reconciliation, and the monitor's automatic retry is proven in the
        # retry tests above.
        monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 3600.0)
        monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.05)
        d2 = Daemon(harnesses={})
        HARNESSES[harness.name] = harness
        d2.runtime_io = io
        d2.spawner.runtime_io = io
        harness.store = d2.store
        harness.resume_gate = asyncio.Event()
        start_task = asyncio.create_task(d2.start())
        await wait_until(
            lambda: any(event[0] == "open_parked" for event in harness.events),
            what="the startup re-adoption parking on the gate",
        )

        # Teardown the generation-1 backend/runtime, bump the persisted binding, install the
        # generation-2 runtime, and register its source exactly as its lifecycle does.
        await d2.runtime_manager.teardown(p.id, backend_generation=1)
        d2.store.upsert_runtime_binding(
            replace(binding, backend_generation=2, native_session_id="gen-2-session")
        )
        gen2 = _replacement_runtime(p.id)
        await d2.runtime_manager.get_or_create(p.id, backend_generation=2, create=fake_create(gen2))
        d2.observer.live.register(
            LiveRegistration(
                participant_id=p.id,
                live_source=gen2.live_source(),
                channel=harness.runtime.channel,
                backend_generation=2,
                native_session_id="gen-2-session",
                evidence_sink=d2.controls.record_terminal_evidence,
                active_job_for_turn=d2.controls.active_job_for_native_turn,
            )
        )
        registration_before = d2.observer.live.registration_for(p.id)
        assert registration_before is not None
        yield SimpleNamespace(
            d2=d2,
            p=p,
            harness=harness,
            gen2=gen2,
            registration_before=registration_before,
            start_task=start_task,
        )
    finally:
        harness.resume_gate = None
        harness.resume_fails = False
        if d2 is not None:
            await d2.aclose()


async def test_startup_resume_race_never_closes_or_unregisters_a_successor(
    theater_home, terminal_provider, monkeypatch
):
    """A startup completion that lost its generation may not touch the successor.

    The stale startup completion must fail closed without closing the successor runtime,
    unregistering its live source, or rewriting the persisted binding.
    """
    async with _startup_parked_behind_a_successor(terminal_provider, monkeypatch) as race:
        race.harness.resume_gate.set()
        await asyncio.wait_for(race.start_task, 5.0)

        # The stale startup completion failed closed and touched nothing the
        # successor owns.
        assert race.d2.runtime_manager.get(race.p.id) is race.gen2, "generation 2 remains current"
        assert race.gen2.state.closed is False, "the successor runtime is never closed"
        snapshot = await race.gen2.snapshot()
        assert snapshot.health is ConnectionHealth.CONNECTED
        registration = race.d2.observer.live.registration_for(race.p.id)
        assert registration is race.registration_before, "the successor's wiring is never replaced"
        assert registration.backend_generation == 2
        assert registration.native_session_id == "gen-2-session"
        current = race.d2.store.get_runtime_binding(race.p.id)
        assert current is not None
        assert current.backend_generation == 2, "the stale path never rewrites the binding"
        assert current.native_session_id == "gen-2-session"


async def test_startup_success_race_never_registers_over_a_successor(
    theater_home, terminal_provider, monkeypatch
):
    """A successful stale re-adoption must not overwrite the successor.

    The parked generation-1 open SUCCEEDS after generation 2 replaced the binding, runtime
    and live registration: the stale success must revalidate to nothing and register nothing.
    """
    async with _startup_parked_behind_a_successor(terminal_provider, monkeypatch) as race:
        # The parked generation-1 open now SUCCEEDS — after the replacement.
        race.harness.resume_fails = False
        race.harness.resume_gate.set()
        await asyncio.wait_for(race.start_task, 5.0)
        assert any(
            event[0] == "open_session" and event[1] is SessionOpenMode.RECONNECT
            for event in race.harness.events
        ), "the parked generation-1 open completed successfully"

        # The stale success registered nothing over the successor.
        assert race.d2.runtime_manager.get(race.p.id) is race.gen2, "generation 2 remains current"
        assert race.gen2.state.closed is False, "the successor runtime is never closed"
        snapshot = await race.gen2.snapshot()
        assert snapshot.health is ConnectionHealth.CONNECTED
        registration = race.d2.observer.live.registration_for(race.p.id)
        assert registration is race.registration_before, "no stale source is registered"
        assert registration.live_source is race.gen2.live_source()
        assert registration.backend_generation == 2
        assert registration.native_session_id == "gen-2-session"
        current = race.d2.store.get_runtime_binding(race.p.id)
        assert current is not None
        assert current.backend_generation == 2, "the stale success never rewrites the binding"
        assert current.native_session_id == "gen-2-session"


async def test_startup_discarded_candidate_close_cancels_the_monitor(
    theater_home, terminal_provider, monkeypatch
):
    """Daemon shutdown cancels the retry monitor: no post-close recovery.

    The verified backend survives the close.
    """
    io = RoutingIO()
    harness = _ScriptedResumeHarness()
    d1 = await native_daemon(io, harness, terminal_provider)
    d2 = None
    p = None
    backend_pid = None
    try:
        p = await spawn_native(d1, spawn_request(prompt=""))
        binding = d1.store.get_runtime_binding(p.id)
        assert binding is not None
        backend_pid = binding.backend_pid
        assert backend_pid is not None
        await d1.aclose()

        monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.05)
        monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.05)
        d2 = await _restart_daemon(io, harness, terminal_provider)
        candidate = d2.runtime_manager.get(p.id)
        assert candidate is not None
        assert (await candidate.snapshot()).health is ConnectionHealth.DISCONNECTED

        await d2.aclose()
        assert d2.runtime_manager._monitors == {}, "every owned monitor is awaited"

        # The transient failure clears after the close: no recovery may run.
        harness.resume_fails = False
        await asyncio.sleep(0.25)
        assert d2.runtime_manager.get(p.id) is None, "no runtime is reinstalled after close"
        assert d2.observer.live.registration_for(p.id) is None
        assert pid_alive(backend_pid), "the close never terminates the backend"
    finally:
        if d2 is not None:
            await d2.aclose()
        if backend_pid is not None and pid_alive(backend_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(backend_pid, signal.SIGKILL)
            await await_reaped(backend_pid)
