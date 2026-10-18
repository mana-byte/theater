"""Native-runtime spawn: backend-first launch order, legacy fallbacks and readiness failures."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.native_lifecycle import (
    NativeFixtureHarness,
    RecordingRuntime,
    RoutingIO,
    init_repo,
    native_daemon,
    spawn_native,
    spawn_request,
    spy_on_backend_launch,
    teardown_backend,
)
from tests.support.runtime_processes import (
    await_reaped,
    pid_alive,
)
from tests.support.waiting import wait_until
from theater import paths
from theater.daemon.controls.service import INTERRUPTED_ERROR_CODE
from theater.daemon.runtime import recovery
from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.spawning import native as native_mod
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    RuntimeLifecyclePhase,
    RuntimeSessionOrder,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.models import BadRequest, HumanPresent, JobState, Status
from theater.provenance import TranscriptProvenance

pytestmark = pytest.mark.usefixtures("native_harness_binary")


class _FailingFrontendPlanRuntime(RecordingRuntime):
    async def frontend_plan(self, *, native_session_id=None):
        self._events.append(("frontend_plan", native_session_id))
        raise ConnectionError("fake frontend plan failed")


class _McpOverlayHarness(NativeFixtureHarness):
    """A runtime harness whose backend plan renders the generic MCP overlay."""

    def overlay_mcp(
        self,
        plan: LaunchPlan,
        *,
        participant_id: str,
        config_path: Path,
        mcp_servers=(),
    ) -> LaunchPlan:
        self.events.append(
            ("overlay_mcp", participant_id, config_path, tuple(s.name for s in mcp_servers))
        )
        return replace(
            plan,
            argv=[*plan.argv, "--mcp-config", str(config_path)],
            env={**plan.env, "NATIVE_FIXTURE_MCP": "rendered"},
            files={**plan.files, config_path: "{}"},
        )


def _pinned(command: list[str]) -> list[str]:
    """Launch pinning resolves the binary to its full path; match the bare name."""
    return [Path(command[0]).name, *command[1:]]


# ---- new spawn: the backend starts before the UI ---------------------------


async def test_new_spawn_persists_intent_before_backend_and_never_puts_prompt_in_argv(
    theater_home, terminal_provider, native_rig, caplog
):
    caplog.set_level("INFO", logger="theater.timing")
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    try:
        p = await spawn_native(d, spawn_request(prompt="do the thing"))
        stages = [row.message for row in caplog.records if row.message.startswith("spawn.native_")]
        assert [line.split()[0] for line in stages] == [
            "spawn.native_backend",
            "spawn.native_endpoint",
            "spawn.native_runtime",
            "spawn.native_frontend_plan",
            "spawn.native_session",
        ]
        assert all(f"id={p.id} operation=" in line for line in stages)

        # The pane runs the promptless native UI; backend and pane argv never
        # contain the prompt (the pane plan is the only window ever created).
        assert len(terminal_provider.creations) == 1, "no second UI may be launched"
        command = terminal_provider.creations[0]["command"]
        assert _pinned(command)[0:2] == ["native-fixture", "--remote"]
        assert command[2] == wiring_mod.native_endpoint(p.id)
        assert "do the thing" not in command

        binding = d.store.get_runtime_binding(p.id)
        assert binding is not None
        assert binding.lifecycle is RuntimeLifecyclePhase.ACTIVE
        assert binding.native_session_id is not None
        # The exact UI-created session, adopted as the resume identity.
        runtime = d.runtime_manager.get(p.id)
        assert isinstance(runtime, RecordingRuntime)
        assert binding.native_session_id == runtime.state.native_session_id
        persisted = d.store.get_participant(p.id)
        assert persisted.session_id == binding.native_session_id
        assert persisted.session_correlation == str(TranscriptProvenance.EXACT)

        # The verified backend identity is persisted and the process is live.
        assert binding.backend_pid is not None
        assert pid_alive(binding.backend_pid)

        # The initial prompt went through the control service exactly once —
        # never pasted into the pane.
        assert runtime.state.sent == ["do the thing"]
        assert terminal_provider.deliveries == []

        # Order of operations: the intent is durable before the backend plan
        # is even asked for; the promptless UI plan precedes session
        # discovery; the initial prompt is the last thing the sequence does.
        assert native_rig.harness.events == [
            ("legacy_plan", "do the thing"),
            ("plan", RuntimeLifecyclePhase.INTENDED),
            ("frontend_plan", None),
            ("open_session", SessionOpenMode.NEW, None, binding.native_session_id),
            ("send", "do the thing"),
        ]

        # The send job exists and is running; evidence routing is follow-on
        # work, so nothing more may be claimed here.
        running = d.store.running_jobs_for_target(p.id)
        assert [job.prompt for job in running] == ["do the thing"]
    finally:
        if p is not None:
            await teardown_backend(d, p.id)
        await d.aclose()


# ---- wiring selection -------------------------------------------------------


async def test_auto_selection_rollback_keeps_every_spawn_legacy(
    theater_home, terminal_provider, monkeypatch
):
    """The rollback path: the gate constant flipped back to `False`.

    Pins the rollback contract: `auto` spawns select legacy.
    """
    harness = NativeFixtureHarness()
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", False)
    d = await native_daemon(RoutingIO(), harness, terminal_provider)
    try:
        p = await spawn_native(d, spawn_request())
        assert d.store.get_runtime_binding(p.id) is None, "legacy keeps no binding row"
        assert _pinned(terminal_provider.creations[0]["command"]) == [
            "native-fixture",
            "--prompt",
            "do the task",
        ]
        assert d.runtime_manager.get(p.id) is None
        assert harness.events == [("legacy_plan", "do the task")]
    finally:
        await d.aclose()


async def test_explicit_legacy_opts_out_of_native_wiring(
    theater_home, terminal_provider, native_rig
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    try:
        p = await spawn_native(d, spawn_request(wiring=RuntimeWiring.LEGACY))
        assert d.store.get_runtime_binding(p.id) is None
        assert d.runtime_manager.get(p.id) is None
        assert native_rig.harness.events == [("legacy_plan", "do the task")]
    finally:
        await d.aclose()


@pytest.mark.parametrize(
    "harness_options",
    [
        pytest.param({"supported": False}, id="unsupported_manifest"),
        pytest.param({"with_runtime": False}, id="no_runtime_manifest"),
    ],
)
async def test_harness_without_native_runtime_support_is_legacy_for_every_native_preference(
    theater_home, terminal_provider, monkeypatch, harness_options
):
    harness = NativeFixtureHarness(**harness_options)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await native_daemon(RoutingIO(), harness, terminal_provider)
    try:
        p = await spawn_native(d, spawn_request())
        assert d.store.get_runtime_binding(p.id) is None
        assert harness.events == [("legacy_plan", "do the task")]

        explicit = await spawn_native(
            d, spawn_request(prompt="explicit", wiring=RuntimeWiring.NATIVE)
        )
        assert d.store.get_runtime_binding(explicit.id) is None
        assert len(terminal_provider.creations) == 2
        assert harness.events == [
            ("legacy_plan", "do the task"),
            ("legacy_plan", "explicit"),
        ]
    finally:
        await d.aclose()


# ---- fork ----------------------------------------------------------------------


async def test_fork_opens_the_exact_parent_session_then_attaches_the_ui_to_its_result(
    theater_home, terminal_provider, native_rig
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    parent = None
    fork = None
    try:
        parent = await spawn_native(d, spawn_request(prompt="first turn"))
        parent_binding = d.store.get_runtime_binding(parent.id)
        d.registry.mark_dead(parent.id)

        fork = await spawn_native(
            d,
            spawn_request(prompt="fork work", resume=parent_binding.native_session_id),
        )
        fork_binding = d.store.get_runtime_binding(fork.id)

        # FORK opens the predecessor's exact session first; the UI attaches
        # to the exact returned id, and the prompt never rides the argv.
        assert (
            "open_session",
            SessionOpenMode.FORK,
            parent_binding.native_session_id,
            fork_binding.native_session_id,
        ) in native_rig.harness.events
        assert fork_binding.native_session_id != parent_binding.native_session_id
        assert fork.session_id == fork_binding.native_session_id
        command = terminal_provider.creations[-1]["command"]
        assert fork_binding.native_session_id in command
        assert "fork work" not in command

        runtime = d.runtime_manager.get(fork.id)
        assert runtime.state.sent == ["fork work"]
        assert fork_binding.lifecycle is RuntimeLifecyclePhase.ACTIVE
        # The parent's identity is untouched by the fork; a dead parent's binding may already be
        # torn down by the daemon, so only a surviving one is compared.
        survivor = d.store.get_runtime_binding(parent.id)
        assert survivor is None or survivor.native_session_id == parent_binding.native_session_id
    finally:
        for pid_owner in (parent, fork):
            if pid_owner is not None:
                await teardown_backend(d, pid_owner.id)
        await d.aclose()


# ---- live wiring --------------------------------------------------------------


async def test_new_spawn_registers_live_wiring_and_teardown_unregisters(
    theater_home, terminal_provider, native_rig
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    try:
        p = await spawn_native(d, spawn_request(prompt="live evidence"))
        binding = d.store.get_runtime_binding(p.id)
        registration = d.observer.live.registration_for(p.id)
        assert registration is not None, "live wiring is registered once identity is bound"
        runtime = d.runtime_manager.get(p.id)
        assert registration.live_source is runtime.live_source()
        assert registration.channel is native_rig.harness.runtime.channel
        assert registration.backend_generation == binding.backend_generation
        assert registration.native_session_id == binding.native_session_id
        assert registration.evidence_sink == d.controls.record_terminal_evidence
        assert registration.active_job_for_turn == d.controls.active_job_for_native_turn

        await recovery.teardown_participant_runtime(d, p.id, caller_id="cli")
        assert d.observer.live.registration_for(p.id) is None, "teardown unregisters"
        await await_reaped(binding.backend_pid)
    finally:
        if p is not None and d.store.get_runtime_binding(p.id) is not None:
            with contextlib.suppress(Exception):
                await teardown_backend(d, p.id)
        await d.aclose()


async def test_teardown_cancels_the_followup_queue_without_presence_gating(
    theater_home, terminal_provider, native_rig, monkeypatch
):
    """Queue cancellation at teardown is Theater-owned: no presence refresh.

    Callers reach teardown with the pane gone, so a gated interrupt would only refuse on
    wake churn or pass on stale facts; the queue must cancel regardless.
    """
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    try:
        p = await spawn_native(d, spawn_request(prompt="queue work"))
        job = await d.controls.queue_followup(p.id, caller_id="cli", prompt="later")
        assert d.store.queued_control_operation_count(p.id) == 1
        binding = d.store.get_runtime_binding(p.id)
        assert binding is not None

        async def refusing_presence(participant_id):
            raise HumanPresent(
                f"teardown must not refresh presence for {participant_id!r}: "
                "the queue is Theater-owned"
            )

        monkeypatch.setattr(d.presence, "require_absent", refusing_presence)
        stopped = await recovery.teardown_participant_runtime(d, p.id, caller_id="cli")
        assert stopped is True
        assert d.store.queued_control_operation_count(p.id) == 0
        finished = d.store.get_job(job.handle)
        assert finished is not None
        assert finished.state == JobState.KILLED.value
        assert finished.error_code == INTERRUPTED_ERROR_CODE
        assert d.store.get_runtime_binding(p.id) is None
        await await_reaped(binding.backend_pid)
    finally:
        if p is not None and d.store.get_runtime_binding(p.id) is not None:
            with contextlib.suppress(Exception):
                await teardown_backend(d, p.id)
        await d.aclose()


async def test_backend_plan_receives_the_participant_scoped_mcp_config(
    theater_home, terminal_provider, monkeypatch
):
    io = RoutingIO()
    harness = _McpOverlayHarness()
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await native_daemon(io, harness, terminal_provider)
    plans: dict = {}
    real_launch = d.runtime_manager.launch_backend

    async def launch_spy(participant_id, **kwargs):
        plans["backend"] = kwargs["plan"].backend
        return await real_launch(participant_id, **kwargs)

    d.runtime_manager.launch_backend = launch_spy
    p = None
    try:
        p = await spawn_native(d, spawn_request(prompt="use the tools"))
        config_path = paths.mcp_config_path(p.id)
        # The overlay ran once, on the backend plan, with Theater's two
        # participant-scoped endpoints and the participant's config path.
        assert (
            harness.events.count(("overlay_mcp", p.id, config_path, ("theater", "theater_wait")))
            == 1
        )
        backend = plans["backend"]
        assert backend.argv[-2:] == ["--mcp-config", str(config_path)]
        assert backend.env["NATIVE_FIXTURE_MCP"] == "rendered"
        assert backend.files[config_path] == "{}"
        # The overlay's plan file was written before the backend launched.
        assert config_path.is_file()
        # The UI pane argv stays the promptless frontend plan: the overlay
        # never leaks into the pane, and the prompt never rides any argv.
        command = terminal_provider.creations[0]["command"]
        assert "--mcp-config" not in command
        assert "use the tools" not in command
    finally:
        if p is not None:
            await teardown_backend(d, p.id)
        await d.aclose()


async def test_initial_prompt_binds_the_spawn_job_handle_exactly_once(
    theater_home, terminal_provider, native_rig
):
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    p = None
    try:
        p = await spawn_native(d, spawn_request(prompt="one handle"))
        # The spawn job is the only job for this participant: no second send job exists.
        running = d.store.running_jobs_for_target(p.id)
        assert [job.handle for job in running] == [p.id]
    finally:
        if p is not None:
            await teardown_backend(d, p.id)
        await d.aclose()


async def test_session_first_failure_clears_native_identity_before_fallback(
    theater_home, terminal_provider, monkeypatch
):
    harness = NativeFixtureHarness()
    harness.runtime = replace(
        harness.runtime,
        session_order=RuntimeSessionOrder.SESSION_FIRST,
        factory=lambda context: _FailingFrontendPlanRuntime(context, harness.events),
    )
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await native_daemon(RoutingIO(), harness, terminal_provider)
    launched: dict = {}
    spy_on_backend_launch(d, launched)
    try:
        participant = await spawn_native(d, spawn_request(prompt="legacy once"))
        await wait_until(
            lambda: bool(terminal_provider.creations),
            what="the accepted launch to reach the provider",
        )
        persisted = d.store.get_participant(participant.id)
        assert persisted is not None
        assert persisted.status is not Status.DEAD
        assert persisted.session_id is None
        assert persisted.session_correlation is None
        assert d.store.get_runtime_binding(participant.id) is None
        assert len(terminal_provider.creations) == 1
        assert _pinned(terminal_provider.creations[0]["command"]) == [
            "native-fixture",
            "--prompt",
            "legacy once",
        ]
        open_event = next(event for event in harness.events if event[0] == "open_session")
        assert open_event[1] is SessionOpenMode.NEW
        assert ("frontend_plan", open_event[3]) in harness.events
    finally:
        await d.aclose()


# ---- endpoint readiness: the connect path waits for the backend's bind ----


async def test_no_runtime_connection_before_endpoint_readiness(
    theater_home, terminal_provider, native_rig, monkeypatch
):
    """frontend_plan/open_session cannot begin before the endpoint accepts.

    While the test holds the readiness gate closed, nothing connected; the order is probe, connect.
    """
    d = await native_daemon(native_rig.io, native_rig.harness, terminal_provider)
    gate = asyncio.Event()
    real_wait = native_mod.wait_for_unix_endpoint
    real_create = d.runtime_manager.get_or_create

    async def gated_wait(endpoint: str, *, timeout: float):
        native_rig.harness.events.append(("endpoint_wait", endpoint))
        await gate.wait()
        await real_wait(endpoint, timeout=timeout)
        native_rig.harness.events.append(("endpoint_accepted", endpoint))

    async def create_spy(participant_id, **kwargs):
        runtime = await real_create(participant_id, **kwargs)
        native_rig.harness.events.append(("runtime_created", participant_id))
        return runtime

    monkeypatch.setattr(native_mod, "wait_for_unix_endpoint", gated_wait)
    monkeypatch.setattr(d.runtime_manager, "get_or_create", create_spy)
    task = None
    try:
        task = asyncio.create_task(spawn_native(d, spawn_request(prompt="readiness first")))
        await wait_until(
            lambda: any(event[0] == "endpoint_wait" for event in native_rig.harness.events),
            what="an 'endpoint_wait' launch event",
        )
        # The endpoint is provably accepting (the backend binds at launch),
        # yet nothing downstream of readiness ran while the gate was closed:
        # no runtime instance, no frontend plan, no session open, no connect.
        assert not any(e[0] == "runtime_created" for e in native_rig.harness.events)
        assert not any(e[0] in ("frontend_plan", "open_session") for e in native_rig.harness.events)
        assert native_rig.io.connects == [], "no connection before endpoint readiness"
        gate.set()
        participant = await task
        assert participant.status is not Status.DEAD
        order = [e[0] for e in native_rig.harness.events]
        assert order.index("endpoint_accepted") < order.index("runtime_created")
        assert order.index("endpoint_accepted") < order.index("frontend_plan")
        assert order.index("frontend_plan") < order.index("open_session")
    finally:
        gate.set()
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await task
        for p in d.registry.list():
            with contextlib.suppress(Exception):
                await teardown_backend(d, p.id)
        await d.aclose()


async def test_endpoint_readiness_failure_falls_back_after_verified_cleanup(
    theater_home, terminal_provider, monkeypatch
):
    """A backend that never binds fails at the readiness probe, pre-dispatch-clean.

    Only the probe's deadline is compressed, so the real probe still diagnoses and the
    ordinary pre-dispatch cleanup precedes the ordinary fallback launch.
    """
    io = RoutingIO()
    harness = NativeFixtureHarness(binds=False)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    real_wait = native_mod.wait_for_unix_endpoint
    budgets: list[float] = []

    async def compressed_wait(endpoint: str, *, timeout: float):
        budgets.append(timeout)
        await real_wait(endpoint, timeout=0.3)

    monkeypatch.setattr(native_mod, "wait_for_unix_endpoint", compressed_wait)
    d = await native_daemon(io, harness, terminal_provider)
    launched: dict = {}
    spy_on_backend_launch(d, launched)
    try:
        participant = await spawn_native(d, spawn_request(prompt="never delivered"))
        # The launch hands the probe the single startup deadline; no
        # independent readiness policy exists to reject a slow-but-conforming
        # backend inside the overall bound.
        assert budgets == [native_mod.NATIVE_LAUNCH_DEADLINE_SECONDS]
        await await_reaped(launched.get("pid"))
        assert participant.status is not Status.DEAD
        assert d.store.get_runtime_binding(participant.id) is None
        assert _pinned(terminal_provider.creations[-1]["command"]) == [
            "native-fixture",
            "--prompt",
            "never delivered",
        ]
        assert terminal_provider.deliveries == []
    finally:
        await d.aclose()


async def test_cancelled_endpoint_wait_cannot_escape_backend_ownership(
    theater_home, terminal_provider, monkeypatch
):
    """Cancelling the spawn inside the readiness wait still cleans the backend.

    The daemon alone owns the detached backend: cancellation follows the pre-dispatch
    cleanup (backend, pane and binding go) instead of leaking a process.
    """
    io = RoutingIO()
    harness = NativeFixtureHarness(binds=False)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await native_daemon(io, harness, terminal_provider)
    launched: dict = {}
    spy_on_backend_launch(d, launched)
    entered = asyncio.Event()
    real_wait = native_mod.wait_for_unix_endpoint

    async def wait_spy(endpoint: str, *, timeout: float):
        entered.set()
        await real_wait(endpoint, timeout=timeout)

    monkeypatch.setattr(native_mod, "wait_for_unix_endpoint", wait_spy)
    task = None
    try:
        task = asyncio.create_task(spawn_native(d, spawn_request(prompt="never delivered")))
        await wait_until(entered.is_set, what="the launch to reach the endpoint wait")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        participants = d.registry.list(include_dead=True)
        assert len(participants) == 1
        failed = participants[0]
        assert failed.status is not Status.DEAD
        assert d.store.get_runtime_binding(failed.id) is not None
        assert pid_alive(launched.get("pid"))
        assert terminal_provider.creations == []
        assert terminal_provider.deliveries == []
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        for participant in d.registry.list():
            with contextlib.suppress(Exception):
                await teardown_backend(d, participant.id)
        await d.aclose()


# ---- failure before the launch ---------------------------------------------------


async def test_spawn_rpc_pre_launch_failure_cleans_the_reservation_once(
    theater_home, terminal_provider, tmp_path, monkeypatch
):
    """A failure before Spawner.launch is the RPC's reservation to clean."""
    repo = init_repo(tmp_path / "repo")
    io = RoutingIO()
    harness = NativeFixtureHarness()
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await native_daemon(io, harness, terminal_provider)

    def reserve_refuses(*args, **kwargs):
        raise BadRequest("job table refuses")

    monkeypatch.setattr(ParticipantLaunchService, "_reserve_spawn_job", reserve_refuses)
    try:
        with pytest.raises(BadRequest, match="job table refuses"):
            await spawn_native(d, spawn_request(prompt="never launched", cwd=repo, worktree=True))
        assert d.registry.list(include_dead=True) == []
        assert terminal_provider.creations == []
    finally:
        await d.aclose()
