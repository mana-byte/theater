"""Wave 3A: runtime-lifecycle integration — spawn, restart, shutdown, kill.

The daemon composes ``HarnessRuntimeManager``, ``WebSocketRuntimeIO``, and
``ControlService`` into its lifecycle; these tests drive that composition
end to end against the frozen fake runtime in ``tests/rig/fake_runtime.py``
with *real* detached backend processes (short-lived ``python -c sleep``
children) and the fake tmux rig, on an isolated ``THEATER_HOME``.

What is deliberately not proven here: anything about a real native CLI or a
real tmux server. The backend process is real (launch, adopt, teardown all
verify a live pid), the UI pane is the fake tmux's, and the runtime is the
contract-faithful in-memory fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeConnection, FakeRuntimeState
from theater import paths
from theater.daemon.harness_runtime import endpoint_to_path
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
)
from theater.daemon.rpc import participants as participants_mod
from theater.daemon.runtime import recovery
from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.server import Daemon
from theater.daemon.spawning import native as native_mod
from theater.daemon.spawning.models import SpawnRequest
from theater.harness import HARNESSES, Harness
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    RuntimeCompatibility,
    RuntimeConnectionError,
    RuntimeContext,
    RuntimeIO,
    RuntimeLifecyclePhase,
    RuntimeManifest,
    RuntimePlan,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.harness.observation import TranscriptObserver
from theater.models import BadRequest, Status, TheaterError
from theater.provenance import TranscriptProvenance

SLEEP_SNIPPET = "import time; time.sleep(300)"

#: The fake backend binds the private endpoint socket before sleeping, the
#: way the stock native backend measurably binds only after exec: the
#: production launch waits for that bind, so the rig must really do it.
BIND_SLEEP_SNIPPET = """import socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind({path!r})
s.listen(1)
time.sleep(300)
"""


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _await_reaped(pid: int | None, timeout: float = 5.0) -> None:
    """Wait until a terminated process is reaped and its pid is gone."""
    for _ in range(int(timeout / 0.02)):
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"pid {pid} is still alive; the backend was not terminated")


async def _await_event(harness: _Harness, tag: str, *, timeout: float = 5.0) -> None:
    """Bounded wait until the harness records one launch event tag."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not any(event[0] == tag for event in harness.events):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for a {tag!r} launch event")
        await asyncio.sleep(0.01)


async def _await_flag(flag: asyncio.Event, *, what: str, timeout: float = 5.0) -> None:
    """Bounded wait for one asyncio flag; a hang detector, never a sleep."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not flag.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.01)


class _Obs(TranscriptObserver):
    """A transcript-less observer; observation is disabled in these tests."""

    has_transcript = False

    def find_transcript(self, *, cwd, session_id=None, after=None):  # pragma: no cover
        return None

    def session_id(self, transcript):  # pragma: no cover
        return None

    def parse(self, line, index, *, clip_text=True):  # pragma: no cover
        return []

    def is_idle_screen(self, capture):  # pragma: no cover
        return False


class _RoutingIO(RuntimeIO):
    """One shared I/O whose connections never leave the process.

    The fake runtime is in-memory, so a connection is only a record that the
    endpoint was reachable.
    """

    def __init__(self) -> None:
        self.connects: list[str] = []

    async def connect(self, endpoint: str, *, timeout: float):
        del timeout
        self.connects.append(endpoint)
        return FakeRuntimeConnection(FakeRuntimeState(participant_id="connection"))


class _RecordingRuntime(FakeRuntime):
    """The fake runtime with order-of-operations evidence."""

    def __init__(self, context: RuntimeContext, events: list) -> None:
        super().__init__(context)
        self._events = events
        self.context_harness = "wave3a-native"

    async def open_session(self, *, mode, native_session_id=None):
        binding = await super().open_session(mode=mode, native_session_id=native_session_id)
        self._events.append(("open_session", mode, native_session_id, binding.native_session_id))
        return binding

    async def frontend_plan(self, *, native_session_id=None):
        plan = await super().frontend_plan(native_session_id=native_session_id)
        self._events.append(("frontend_plan", native_session_id))
        # The UI binary is the harness binary, so the pane identity check
        # (which detects the harness from the pane's foreground command)
        # verifies this pane as ours.
        return LaunchPlan(argv=[self.context_harness, *plan.argv[1:]])

    async def send(self, *, operation_id, prompt):
        receipt = await super().send(operation_id=operation_id, prompt=prompt)
        self._events.append(("send", prompt))
        return receipt


class _FailingOpenRuntime(FakeRuntime):
    """Fails session discovery, simulating a dead private endpoint."""

    async def open_session(self, *, mode, native_session_id=None):
        raise ConnectionError("fake backend refused the session open")


class _StalledOpenRuntime(FakeRuntime):
    """Session discovery never resolves, simulating a stalled startup."""

    async def open_session(self, *, mode, native_session_id=None):
        if mode is SessionOpenMode.NEW:
            await asyncio.Event().wait()
        return await super().open_session(mode=mode, native_session_id=native_session_id)


class _Harness(Harness):
    """A runtime-capable test harness; ``plan_launch`` is the legacy path."""

    name = "wave3a-native"
    binary = "wave3a-native"
    icon = "W"
    launch_parameter_support = LaunchParameterSupport(model=True, resume=True)
    #: A fork delivers its prompt through the control service, not the argv.
    resume_takes_prompt = True

    def __init__(
        self,
        *,
        supported: bool = True,
        open_fails: bool = False,
        open_stalls: bool = False,
        with_runtime: bool = True,
        binds: bool = True,
    ):
        self.observer = _Obs()
        self.events: list = []
        #: Store handle for order assertions; wired by ``_daemon``.
        self.store = None
        self._supported = supported
        self._open_fails = open_fails
        self._open_stalls = open_stalls
        #: Whether the fake backend binds its private endpoint before
        #: sleeping; ``False`` simulates a backend that never becomes ready.
        self._binds = binds
        self.runtime = self._manifest() if with_runtime else None

    def _manifest(self) -> RuntimeManifest:
        harness = self

        def probe(context) -> RuntimeCompatibility:
            return RuntimeCompatibility(
                supported=harness._supported,
                policy="wave3a-verified",
                native_version="9.9.9",
            )

        def plan(context) -> RuntimePlan:
            binding = harness.store.get_runtime_binding(context.participant_id)
            harness.events.append(("plan", binding.lifecycle if binding is not None else None))
            snippet = (
                BIND_SLEEP_SNIPPET.format(path=str(endpoint_to_path(context.endpoint)))
                if harness._binds
                else SLEEP_SNIPPET
            )
            return RuntimePlan(
                backend=LaunchPlan(argv=[sys.executable, "-c", snippet]),
                endpoint=context.endpoint,
            )

        def factory(context: RuntimeContext):
            if harness._open_fails:
                return _FailingOpenRuntime(context)
            if harness._open_stalls:
                return _StalledOpenRuntime(context)
            return _RecordingRuntime(context, harness.events)

        return RuntimeManifest(
            probe=probe,
            plan=plan,
            factory=factory,
            channel=LiveChannelDeclaration(
                channel=ChannelDeclaration(
                    id="live",
                    kind=ChannelKind.LIVE,
                    capabilities=(
                        ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
                        ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),
                    ),
                )
            ),
        )

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        mcp_servers=(),
    ) -> LaunchPlan:
        self.events.append(("legacy_plan", prompt))
        return LaunchPlan(argv=[self.binary, "--prompt", prompt])


class _McpOverlayHarness(_Harness):
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
            env={**plan.env, "WAVE3A_MCP": "rendered"},
            files={**plan.files, config_path: "{}"},
        )


def _request(**kwargs) -> SpawnRequest:
    kwargs.setdefault("harness", "wave3a-native")
    kwargs.setdefault("prompt", "do the wave")
    kwargs.setdefault("cwd", "/tmp")
    kwargs.setdefault("approval", "manual")
    return SpawnRequest(**kwargs)


async def _spawn(daemon: Daemon, req: SpawnRequest):
    """Spawn through the actual spawn RPC — the composition users hit.

    The RPC creates the spawn job (its handle is the participant id) between
    reserve and launch and owns the pre-launch reservation cleanup, so this
    exercises the real native dispatch and failure control flow. Returns the
    registry's live participant row.
    """
    from theater.daemon.rpc import spawning as spawning_rpc

    result = await spawning_rpc._spawn(
        daemon,
        {
            "harness": req.harness,
            "prompt": req.prompt,
            "cwd": req.cwd,
            "approval": req.approval,
            "parent_id": req.parent_id,
            "tmux_session": req.tmux_session,
            "window_name": req.window_name,
            "background": req.background,
            "worktree": req.worktree,
            "base_branch": req.base_branch,
            "model": req.model,
            "reasoning_effort": req.reasoning_effort,
            "resume": req.resume,
            "name": req.name,
            "description": req.description,
        },
    )
    return daemon.registry.get(result["handle"])


async def _daemon(io: _RoutingIO, harness: _Harness, fake_tmux) -> Daemon:
    # The fake tmux pre-declares %1-%3 as live "vibe" panes; the first spawned
    # window also draws pane id %1, and the pane identity check would read the
    # pre-declared row instead of ours. These tests track only their own panes.
    fake_tmux.visible_panes.clear()
    d = Daemon(harnesses={})
    # ``Daemon.__init__`` re-installs the shipped harness registry, so the
    # test harness registers itself after construction (``clean_registry``
    # still restores the shipped set when the test ends).
    HARNESSES[harness.name] = harness
    # The composition root injects one shared runtime I/O; the test swaps it
    # for the in-process routing fake the same way.
    d.runtime_io = io
    d.spawner.runtime_io = io
    harness.store = d.store
    await d.start()
    return d


def _launch_spy(daemon: Daemon, captured: dict) -> None:
    """Record the real backend pid at launch, before any cleanup can delete it."""
    real_launch = daemon.runtime_manager.launch_backend

    async def launch_spy(participant_id, **kwargs):
        identity = await real_launch(participant_id, **kwargs)
        captured["pid"] = identity.pid
        return identity

    daemon.runtime_manager.launch_backend = launch_spy


async def _teardown(daemon: Daemon, pid: str) -> None:
    with contextlib.suppress(Exception):
        await daemon.runtime_manager.teardown(pid, backend_generation=1)


@pytest.fixture
def rig(monkeypatch):
    """The native environment: shared I/O, test harness, auto gate enabled."""
    io = _RoutingIO()
    harness = _Harness()
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    return SimpleNamespace(io=io, harness=harness)


@pytest.fixture(autouse=True)
def _control_send_job_handle_compat(monkeypatch):
    """Bridge ``ControlService.send`` until the control correction lands.

    The frozen native contract sends the initial prompt through
    ``ControlService.send(..., job_handle=participant.id)`` so the spawn RPC
    job is the one bound to native terminal evidence and no second send job
    exists. That additive ``job_handle`` parameter is the control worker's
    cross-track correction; until it lands, this shim records the requested
    handle and delegates with the pre-correction signature. When the real
    signature accepts ``job_handle``, the shim stands down and the
    production path is exercised directly.
    """
    from theater.daemon.controls import service as controls_service_mod

    parameters = inspect.signature(controls_service_mod.ControlService.send).parameters
    if "job_handle" in parameters:
        return

    real_send = controls_service_mod.ControlService.send

    @functools.wraps(real_send)
    async def send_with_handle_shim(self, participant_id, **kwargs):
        job_handle = kwargs.pop("job_handle", None)
        self.__dict__.setdefault("_test_job_handle_sends", []).append((participant_id, job_handle))
        return await real_send(self, participant_id, **kwargs)

    monkeypatch.setattr(controls_service_mod.ControlService, "send", send_with_handle_shim)


def _init_repo(path: Path) -> str:
    """A real git repo with one commit, for worktree-retirement assertions."""
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return str(path)


# ---- UI-first NEW order ---------------------------------------------------


async def test_new_spawn_persists_intent_before_backend_and_never_puts_prompt_in_argv(
    theater_home, fake_tmux, rig
):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = None
    try:
        p = await _spawn(d, _request(prompt="do the thing"))

        # The pane runs the promptless native UI; backend and pane argv never
        # contain the prompt (the pane plan is the only window ever created).
        assert len(fake_tmux.windows) == 1, "no second UI may be launched"
        command = fake_tmux.windows[0]["command"]
        assert command[0:2] == ["wave3a-native", "--remote"]
        assert command[2] == wiring_mod.native_endpoint(p.id)
        assert "do the thing" not in command

        binding = d.store.get_runtime_binding(p.id)
        assert binding is not None
        assert binding.lifecycle is RuntimeLifecyclePhase.ACTIVE
        assert binding.native_session_id is not None
        # The exact UI-created session, adopted as the resume identity.
        runtime = d.runtime_manager.get(p.id)
        assert isinstance(runtime, _RecordingRuntime)
        assert binding.native_session_id == runtime.state.native_session_id
        persisted = d.store.get_participant(p.id)
        assert persisted.session_id == binding.native_session_id
        assert persisted.session_correlation == str(TranscriptProvenance.EXACT)

        # The verified backend identity is persisted and the process is live.
        assert binding.backend_pid is not None
        assert _pid_alive(binding.backend_pid)

        # The initial prompt went through the control service exactly once —
        # never pasted into the pane.
        assert runtime.state.sent == ["do the thing"]
        assert fake_tmux.sent == []

        # Order of operations: the intent is durable before the backend plan
        # is even asked for; the promptless UI plan precedes session
        # discovery; the initial prompt is the last thing the sequence does.
        assert rig.harness.events == [
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
            await _teardown(d, p.id)
        await d.aclose()


# ---- wiring selection -------------------------------------------------------


async def test_auto_selection_disabled_by_default_keeps_every_spawn_legacy(
    theater_home, fake_tmux, monkeypatch
):
    harness = _Harness()
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", False)
    d = await _daemon(_RoutingIO(), harness, fake_tmux)
    try:
        p = await d.spawner.spawn(_request())
        assert d.store.get_runtime_binding(p.id) is None, "legacy keeps no binding row"
        assert fake_tmux.windows[0]["command"] == ["wave3a-native", "--prompt", "do the wave"]
        assert d.runtime_manager.get(p.id) is None
        assert harness.events == [("legacy_plan", "do the wave")]
    finally:
        await d.aclose()


async def test_explicit_legacy_opts_out_of_native_wiring(theater_home, fake_tmux, rig):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    try:
        p = await d.spawner.spawn(_request(wiring=RuntimeWiring.LEGACY))
        assert d.store.get_runtime_binding(p.id) is None
        assert d.runtime_manager.get(p.id) is None
        assert rig.harness.events == [("legacy_plan", "do the wave")]
    finally:
        await d.aclose()


async def test_unsupported_manifest_falls_back_to_legacy_on_auto_but_fails_explicit(
    theater_home, fake_tmux, monkeypatch
):
    harness = _Harness(supported=False)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await _daemon(_RoutingIO(), harness, fake_tmux)
    try:
        p = await d.spawner.spawn(_request())
        assert d.store.get_runtime_binding(p.id) is None
        assert harness.events == [("legacy_plan", "do the wave")]

        with pytest.raises(BadRequest, match="compatibility probe refused"):
            await d.spawner.spawn(_request(prompt="explicit", wiring=RuntimeWiring.NATIVE))
        # The refused spawn never launched anything: still exactly one pane.
        assert len(fake_tmux.windows) == 1
    finally:
        await d.aclose()


async def test_harness_without_runtime_manifest_is_legacy_by_construction(
    theater_home, fake_tmux, monkeypatch
):
    harness = _Harness(with_runtime=False)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await _daemon(_RoutingIO(), harness, fake_tmux)
    try:
        p = await d.spawner.spawn(_request())
        assert d.store.get_runtime_binding(p.id) is None

        with pytest.raises(BadRequest, match="no runtime manifest"):
            await d.spawner.spawn(_request(prompt="explicit", wiring=RuntimeWiring.NATIVE))
    finally:
        await d.aclose()


# ---- FORK --------------------------------------------------------------------


async def test_fork_opens_the_exact_parent_session_then_attaches_the_ui_to_its_result(
    theater_home, fake_tmux, rig
):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    parent = None
    fork = None
    try:
        parent = await _spawn(d, _request(prompt="first turn"))
        parent_binding = d.store.get_runtime_binding(parent.id)
        d.registry.mark_dead(parent.id)

        fork = await _spawn(
            d,
            _request(prompt="fork work", resume=parent_binding.native_session_id),
        )
        fork_binding = d.store.get_runtime_binding(fork.id)

        # FORK opens the predecessor's exact session first; the UI attaches
        # to the exact returned id, and the prompt never rides the argv.
        assert (
            "open_session",
            SessionOpenMode.FORK,
            parent_binding.native_session_id,
            fork_binding.native_session_id,
        ) in rig.harness.events
        assert fork_binding.native_session_id != parent_binding.native_session_id
        assert fork.session_id == fork_binding.native_session_id
        command = fake_tmux.windows[-1]["command"]
        assert fork_binding.native_session_id in command
        assert "fork work" not in command

        runtime = d.runtime_manager.get(fork.id)
        assert runtime.state.sent == ["fork work"]
        assert fork_binding.lifecycle is RuntimeLifecyclePhase.ACTIVE
        # The parent's identity is untouched by the fork.
        assert (
            d.store.get_runtime_binding(parent.id).native_session_id
            == parent_binding.native_session_id
        )
    finally:
        for pid_owner in (parent, fork):
            if pid_owner is not None:
                await _teardown(d, pid_owner.id)
        await d.aclose()


# ---- live wiring --------------------------------------------------------------


async def test_new_spawn_registers_live_wiring_and_teardown_unregisters(
    theater_home, fake_tmux, rig
):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = None
    try:
        p = await _spawn(d, _request(prompt="live evidence"))
        binding = d.store.get_runtime_binding(p.id)
        registration = d.observer.live.registration_for(p.id)
        assert registration is not None, "live wiring is registered once identity is bound"
        runtime = d.runtime_manager.get(p.id)
        assert registration.live_source is runtime.live_source()
        assert registration.channel is rig.harness.runtime.channel
        assert registration.backend_generation == binding.backend_generation
        assert registration.native_session_id == binding.native_session_id
        assert registration.evidence_sink == d.controls.record_terminal_evidence
        assert registration.active_job_for_turn == d.controls.active_job_for_native_turn

        await recovery.teardown_participant_runtime(d, p.id, caller_id="cli")
        assert d.observer.live.registration_for(p.id) is None, "teardown unregisters"
        await _await_reaped(binding.backend_pid)
    finally:
        if p is not None and d.store.get_runtime_binding(p.id) is not None:
            with contextlib.suppress(Exception):
                await _teardown(d, p.id)
        await d.aclose()


async def test_backend_plan_receives_the_participant_scoped_mcp_config(
    theater_home, fake_tmux, monkeypatch
):
    io = _RoutingIO()
    harness = _McpOverlayHarness()
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await _daemon(io, harness, fake_tmux)
    plans: dict = {}
    real_launch = d.runtime_manager.launch_backend

    async def launch_spy(participant_id, **kwargs):
        plans["backend"] = kwargs["plan"].backend
        return await real_launch(participant_id, **kwargs)

    d.runtime_manager.launch_backend = launch_spy
    p = None
    try:
        p = await _spawn(d, _request(prompt="use the tools"))
        config_path = paths.mcp_config_path(p.id)
        # The overlay ran once, on the backend plan, with Theater's two
        # participant-scoped endpoints and the participant's config path.
        assert (
            harness.events.count(("overlay_mcp", p.id, config_path, ("theater", "theater_wait")))
            == 1
        )
        backend = plans["backend"]
        assert backend.argv[-2:] == ["--mcp-config", str(config_path)]
        assert backend.env["WAVE3A_MCP"] == "rendered"
        assert backend.files[config_path] == "{}"
        # The overlay's plan file was written before the backend launched.
        assert config_path.is_file()
        # The UI pane argv stays the promptless frontend plan: the overlay
        # never leaks into the pane, and the prompt never rides any argv.
        command = fake_tmux.windows[0]["command"]
        assert "--mcp-config" not in command
        assert "use the tools" not in command
    finally:
        if p is not None:
            await _teardown(d, p.id)
        await d.aclose()


async def test_initial_prompt_binds_the_spawn_job_handle_exactly_once(theater_home, fake_tmux, rig):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = None
    try:
        p = await _spawn(d, _request(prompt="one handle"))
        records = getattr(d.controls, "_test_job_handle_sends", None)
        if records is not None:
            # The control correction has not landed: the compat shim proves
            # the production call site sends job_handle=participant.id
            # exactly once.
            assert records == [(p.id, p.id)]
        else:
            # The real signature accepts job_handle: the spawn job is the
            # only job for this participant — no second send job exists.
            running = d.store.running_jobs_for_target(p.id)
            assert [job.handle for job in running] == [p.id]
    finally:
        if p is not None:
            await _teardown(d, p.id)
        await d.aclose()


# ---- pre-dispatch failure: verified cleanup ---------------------------------


async def test_pre_dispatch_failure_cleans_backend_pane_and_binding(
    theater_home, fake_tmux, monkeypatch
):
    io = _RoutingIO()
    harness = _Harness(open_fails=True)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await _daemon(io, harness, fake_tmux)
    launched: dict = {}
    _launch_spy(d, launched)
    try:
        with pytest.raises(ConnectionError, match="refused the session open"):
            await _spawn(d, _request(prompt="never delivered"))
        # No prompt was ever transmitted; nothing was dispatched.
        participants = d.registry.list(include_dead=True)
        assert len(participants) == 1
        failed = participants[0]
        assert failed.status is Status.DEAD
        assert d.store.get_runtime_binding(failed.id) is None
        await _await_reaped(launched.get("pid"))
        assert failed.tmux_pane not in fake_tmux.panes, "pane killed"
        assert fake_tmux.sent == []
    finally:
        await d.aclose()


# ---- ambiguous dispatch: nothing is cleaned, resent, or relaunched -----------


async def test_failure_after_dispatch_may_have_begun_cleans_nothing(
    theater_home, fake_tmux, rig, monkeypatch
):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = None
    try:
        real_send = d.controls.send

        async def send_then_connection_lost(participant_id, **kwargs):
            await real_send(participant_id, **kwargs)
            raise ConnectionError("connection lost after dispatch")

        monkeypatch.setattr(d.controls, "send", send_then_connection_lost)
        with pytest.raises(ConnectionError, match="connection lost after dispatch"):
            await _spawn(d, _request(prompt="possibly delivered"))
        participants = d.registry.list()
        assert len(participants) == 1
        p = participants[0]
        binding = d.store.get_runtime_binding(p.id)
        assert binding is not None, "the binding survives an ambiguous dispatch"
        assert binding.lifecycle is RuntimeLifecyclePhase.ATTACHED
        assert p.status is not Status.DEAD, "the participant is not cleaned"
        assert _pid_alive(binding.backend_pid), "the backend is not terminated"
        assert p.tmux_pane in fake_tmux.panes, "the UI pane is not killed"
        runtime = d.runtime_manager.get(p.id)
        assert runtime.state.sent == ["possibly delivered"], "sent exactly once, never resent"
    finally:
        if p is not None:
            await _teardown(d, p.id)
        await d.aclose()


# ---- startup timeout: the same pre-dispatch cleanup path ----------------------


async def test_startup_timeout_cleans_verified_resources_before_failing(
    theater_home, fake_tmux, monkeypatch
):
    io = _RoutingIO()
    harness = _Harness(open_stalls=True)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    monkeypatch.setattr(native_mod, "NATIVE_LAUNCH_DEADLINE_SECONDS", 0.2)
    d = await _daemon(io, harness, fake_tmux)
    launched: dict = {}
    _launch_spy(d, launched)
    try:
        with pytest.raises(TheaterError, match="did not complete within"):
            await _spawn(d, _request(prompt="never sent"))
        # The timeout followed the ordinary pre-dispatch cleanup: the
        # backend is terminated, the pane is killed, the binding is gone,
        # and the generic reservation cleanup retired the participant.
        await _await_reaped(launched.get("pid"))
        participants = d.registry.list(include_dead=True)
        assert len(participants) == 1
        failed = participants[0]
        assert failed.status is Status.DEAD
        assert d.store.get_runtime_binding(failed.id) is None
        assert failed.tmux_pane not in fake_tmux.panes, "pane killed"
        assert fake_tmux.sent == []
    finally:
        await d.aclose()


# ---- endpoint readiness: the connect path waits for the backend's bind ----


async def test_no_runtime_connection_before_endpoint_readiness(
    theater_home, fake_tmux, rig, monkeypatch
):
    """frontend_plan/open_session cannot begin before the endpoint accepts.

    The fake backend binds immediately, but the readiness wait is gated by
    the test: while the gate is closed, the launch is provably parked before
    any runtime was created and before anything connected — and the recorded
    order shows the connect path running strictly after the probe accepted.
    """
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    gate = asyncio.Event()
    real_wait = native_mod.wait_for_unix_endpoint
    real_create = d.runtime_manager.get_or_create

    async def gated_wait(endpoint: str, *, timeout: float):
        rig.harness.events.append(("endpoint_wait", endpoint))
        await gate.wait()
        await real_wait(endpoint, timeout=timeout)
        rig.harness.events.append(("endpoint_accepted", endpoint))

    async def create_spy(participant_id, **kwargs):
        runtime = await real_create(participant_id, **kwargs)
        rig.harness.events.append(("runtime_created", participant_id))
        return runtime

    monkeypatch.setattr(native_mod, "wait_for_unix_endpoint", gated_wait)
    monkeypatch.setattr(d.runtime_manager, "get_or_create", create_spy)
    task = None
    try:
        task = asyncio.create_task(_spawn(d, _request(prompt="readiness first")))
        await _await_event(rig.harness, "endpoint_wait")
        # The endpoint is provably accepting (the backend binds at launch),
        # yet nothing downstream of readiness ran while the gate was closed:
        # no runtime instance, no frontend plan, no session open, no connect.
        assert not any(e[0] == "runtime_created" for e in rig.harness.events)
        assert not any(e[0] in ("frontend_plan", "open_session") for e in rig.harness.events)
        assert rig.io.connects == [], "no connection before endpoint readiness"
        gate.set()
        participant = await task
        assert participant.status is not Status.DEAD
        order = [e[0] for e in rig.harness.events]
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
                await _teardown(d, p.id)
        await d.aclose()


async def test_endpoint_readiness_failure_is_bounded_and_cleans_verified_resources(
    theater_home, fake_tmux, monkeypatch
):
    """A backend that never binds fails at the readiness bound, not the deadline.

    The readiness failure carries the endpoint's own diagnostic, fires well
    inside the (unpatched) 30-second launch deadline, and follows the
    ordinary pre-dispatch cleanup: backend terminated, pane killed, binding
    gone, participant retired — no retry, no relaunch.
    """
    io = _RoutingIO()
    harness = _Harness(binds=False)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    monkeypatch.setattr(native_mod, "NATIVE_ENDPOINT_READINESS_SECONDS", 0.3)
    d = await _daemon(io, harness, fake_tmux)
    launched: dict = {}
    _launch_spy(d, launched)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeConnectionError, match="did not accept connections"):
            await _spawn(d, _request(prompt="never delivered"))
        elapsed = time.monotonic() - started
        assert elapsed < native_mod.NATIVE_LAUNCH_DEADLINE_SECONDS, (
            "the readiness failure must be bounded by its own deadline, not the launch deadline"
        )
        await _await_reaped(launched.get("pid"))
        participants = d.registry.list(include_dead=True)
        assert len(participants) == 1
        failed = participants[0]
        assert failed.status is Status.DEAD
        assert d.store.get_runtime_binding(failed.id) is None
        assert failed.tmux_pane not in fake_tmux.panes, "pane killed"
        assert fake_tmux.sent == []
    finally:
        await d.aclose()


async def test_cancelled_endpoint_wait_cannot_escape_backend_ownership(
    theater_home, fake_tmux, monkeypatch
):
    """Cancelling the spawn inside the readiness wait still cleans the backend.

    The backend is a detached process the daemon alone owns; a cancellation
    delivered while the launch is parked on endpoint readiness follows the
    pre-dispatch cleanup path — the backend is terminated, the pane and
    binding go with it — instead of leaking an owned process.
    """
    io = _RoutingIO()
    harness = _Harness(binds=False)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    monkeypatch.setattr(native_mod, "NATIVE_ENDPOINT_READINESS_SECONDS", 5.0)
    d = await _daemon(io, harness, fake_tmux)
    launched: dict = {}
    _launch_spy(d, launched)
    entered = asyncio.Event()
    real_wait = native_mod.wait_for_unix_endpoint

    async def wait_spy(endpoint: str, *, timeout: float):
        entered.set()
        await real_wait(endpoint, timeout=timeout)

    monkeypatch.setattr(native_mod, "wait_for_unix_endpoint", wait_spy)
    task = None
    try:
        task = asyncio.create_task(_spawn(d, _request(prompt="never delivered")))
        await _await_flag(entered, what="the launch to reach the endpoint wait")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _await_reaped(launched.get("pid"))
        participants = d.registry.list(include_dead=True)
        assert len(participants) == 1
        failed = participants[0]
        assert failed.status is Status.DEAD
        assert d.store.get_runtime_binding(failed.id) is None
        assert failed.tmux_pane not in fake_tmux.panes, "pane killed"
        assert fake_tmux.sent == []
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await d.aclose()


# ---- teardown failure: recoverable state is preserved -------------------------


async def test_teardown_failure_preserves_the_worktree_pane_and_binding(
    theater_home, fake_tmux, tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path / "repo")
    io = _RoutingIO()
    harness = _Harness(open_fails=True)
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await _daemon(io, harness, fake_tmux)
    launched: dict = {}
    _launch_spy(d, launched)
    refuse = [False]
    real_teardown = d.runtime_manager.teardown

    async def teardown_spy(participant_id, **kwargs):
        if refuse[0]:
            raise RuntimeError("teardown refused: backend cannot be stopped")
        return await real_teardown(participant_id, **kwargs)

    monkeypatch.setattr(d.runtime_manager, "teardown", teardown_spy)
    try:
        refuse[0] = True
        with pytest.raises(TheaterError, match="teardown also failed"):
            await _spawn(d, _request(prompt="never delivered", cwd=repo, worktree=True))
        # Nothing the backend may still use is reclaimed: the worktree
        # stands, the pane and binding ownership stay, the participant is
        # not marked dead, and the failure is the diagnostic one.
        participants = d.registry.list(include_dead=True)
        assert len(participants) == 1
        failed = participants[0]
        assert failed.status is not Status.DEAD, "the participant keeps ownership"
        assert d.store.get_runtime_binding(failed.id) is not None, "binding kept"
        assert _pid_alive(launched["pid"]), "the backend was not signalled"
        assert failed.tmux_pane in fake_tmux.panes, "pane ownership not erased"
        assert Path(failed.cwd).is_dir(), "the worktree is not retired"
        assert d.observer.live.registration_for(failed.id) is None
    finally:
        # The leaked backend is terminated through the real manager teardown
        # once the refusal stands down; a hard kill is only the fallback.
        refuse[0] = False
        rows = d.registry.list(include_dead=True)
        if rows:
            with contextlib.suppress(Exception):
                await d.runtime_manager.teardown(rows[0].id, backend_generation=1)
        pid = launched.get("pid")
        if pid is not None and _pid_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if pid is not None:
            await _await_reaped(pid)
        await d.aclose()


# ---- shutdown: disconnect only ------------------------------------------------


async def test_shutdown_disconnects_clients_and_leaves_the_backend_alive(
    theater_home, fake_tmux, rig
):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = await _spawn(d, _request(prompt="long turn"))
    binding = d.store.get_runtime_binding(p.id)
    runtime = d.runtime_manager.get(p.id)
    assert runtime.state.connected

    await d.aclose()

    assert runtime.state.connected is False, "the runtime client is disconnected"
    assert _pid_alive(binding.backend_pid), "a healthy backend survives shutdown"
    assert p.tmux_pane in fake_tmux.panes, "the UI survives shutdown"
    # Leave no process behind the test.
    await _teardown(d, p.id)


# ---- restart reconciliation ----------------------------------------------------


async def test_restart_reconnects_the_exact_session_without_a_second_ui(
    theater_home, fake_tmux, rig
):
    d1 = await _daemon(rig.io, rig.harness, fake_tmux)
    p = await d1.spawner.spawn(_request(prompt=""))  # promptless: nothing queued
    binding = d1.store.get_runtime_binding(p.id)
    windows_before = len(fake_tmux.windows)
    await d1.aclose()
    assert _pid_alive(binding.backend_pid)

    # A fresh daemon over the same home reconciles before observation.
    d2 = Daemon(harnesses={})
    HARNESSES[rig.harness.name] = rig.harness
    d2.runtime_io = rig.io
    d2.spawner.runtime_io = rig.io
    rig.harness.store = d2.store
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
        assert isinstance(reconnected, _RecordingRuntime)
        assert reconnected.state.native_session_id == binding.native_session_id
        adopted = d2.store.get_runtime_binding(p.id)
        assert adopted.native_session_id == binding.native_session_id
        assert adopted.backend_pid == binding.backend_pid
        refreshed = d2.registry.get(p.id)
        assert refreshed.session_id == binding.native_session_id
        assert len(fake_tmux.windows) == windows_before, "no second UI is launched"
    finally:
        await _teardown(d2, p.id)
        await d2.aclose()


async def test_restart_reconnect_registers_live_wiring_before_evidence_is_consumed(
    theater_home, fake_tmux, rig
):
    d1 = await _daemon(rig.io, rig.harness, fake_tmux)
    p = await d1.spawner.spawn(_request(prompt=""))  # promptless: nothing queued
    binding = d1.store.get_runtime_binding(p.id)
    await d1.aclose()
    assert _pid_alive(binding.backend_pid)

    d2 = Daemon(harnesses={})
    HARNESSES[rig.harness.name] = rig.harness
    d2.runtime_io = rig.io
    d2.spawner.runtime_io = rig.io
    rig.harness.store = d2.store
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
        assert registration.channel is rig.harness.runtime.channel
        assert registration.backend_generation == binding.backend_generation
        assert registration.native_session_id == binding.native_session_id
        assert registration.evidence_sink == d2.controls.record_terminal_evidence
        assert registration.active_job_for_turn == d2.controls.active_job_for_native_turn
        assert seen_at_evidence == {p.id: registration}, (
            "stored terminal evidence reconciles through the live registration"
        )

        await recovery.teardown_participant_runtime(d2, p.id, caller_id="cli")
        assert d2.observer.live.registration_for(p.id) is None
        await _await_reaped(binding.backend_pid)
    finally:
        if _pid_alive(binding.backend_pid):
            with contextlib.suppress(Exception):
                await _teardown(d2, p.id)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(binding.backend_pid, signal.SIGKILL)
        await d2.aclose()


async def test_restart_fails_unverified_backend_and_marks_the_binding_failed(
    theater_home, fake_tmux, rig
):
    d1 = await _daemon(rig.io, rig.harness, fake_tmux)
    p = await d1.spawner.spawn(_request(prompt=""))
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
    HARNESSES[rig.harness.name] = rig.harness
    d2.runtime_io = rig.io
    d2.spawner.runtime_io = rig.io
    rig.harness.store = d2.store
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
        await _teardown(d1, p.id)


async def test_pre_identity_crash_residue_is_diagnosed_and_never_adopted(theater_home):
    d = Daemon(harnesses={})
    try:
        # Residue of a crash between intent persistence and backend start.
        participant = d.registry.create_spawned(
            harness="wave3a-native", cwd="/tmp", has_prompt=True
        )
        d.store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id=participant.id,
                harness="wave3a-native",
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
    theater_home, fake_tmux, rig
):
    d1 = await _daemon(rig.io, rig.harness, fake_tmux)
    p = await d1.spawner.spawn(_request(prompt=""))
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
    assert _pid_alive(binding.backend_pid)

    d2 = Daemon(harnesses={})
    HARNESSES[rig.harness.name] = rig.harness
    d2.runtime_io = rig.io
    d2.spawner.runtime_io = rig.io
    rig.harness.store = d2.store
    try:
        await d2.start()
        # Ownership adopted (a later kill can terminate it), session not.
        assert d2.runtime_manager.get(p.id) is None, "no runtime is created"
        rows = [row for row in d2.store.bus_tail(limit=50) if row["kind"] == "runtime.orphan"]
        assert rows and "no native session identity" in rows[0]["payload"]["reason"]
        assert d2.runtime_manager.backend(p.id) is not None, "backend ownership adopted"

        # Ownership means termination: the teardown path can still stop it.
        await recovery.teardown_participant_runtime(d2, p.id, caller_id="cli")
        await _await_reaped(binding.backend_pid)
        assert d2.store.get_runtime_binding(p.id) is None
    finally:
        await d2.aclose()


# ---- explicit kill and confirmed exit --------------------------------------------


async def test_explicit_kill_terminates_the_backend_before_worktree_cleanup(
    theater_home, fake_tmux, rig, monkeypatch
):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = None
    try:
        p = await _spawn(d, _request(prompt="work in flight"))
        binding = d.store.get_runtime_binding(p.id)

        teardown_liveness: list[bool] = []
        real_teardown = d.spawner.teardown

        async def teardown_spy(participant, **kwargs):
            teardown_liveness.append(_pid_alive(binding.backend_pid))
            return await real_teardown(participant, **kwargs)

        monkeypatch.setattr(d.spawner, "teardown", teardown_spy)
        await participants_mod._kill(d, {"id": p.id})

        assert teardown_liveness == [False], "backend terminated before pane/worktree teardown"
        assert d.store.get_runtime_binding(p.id) is None
        assert d.registry.get(p.id).status is Status.DEAD
        await _await_reaped(binding.backend_pid)
        assert p.tmux_pane not in fake_tmux.panes
    finally:
        if p is not None:
            await _teardown(d, p.id)
        await d.aclose()


async def test_confirmed_exit_terminates_the_backend_and_sweeps_the_binding(
    theater_home, fake_tmux, rig
):
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = None
    try:
        p = await d.spawner.spawn(_request(prompt=""))
        binding = d.store.get_runtime_binding(p.id)
        # The UI pane exits on its own; the reaper confirms it.
        fake_tmux.remove_pane(p.tmux_pane)
        await d._reap_once()

        assert d.registry.get(p.id).status is Status.DEAD
        assert d.store.get_runtime_binding(p.id) is None
        await _await_reaped(binding.backend_pid)
    finally:
        if p is not None:
            await _teardown(d, p.id)
        await d.aclose()


# ---- round 2: retirement only after a proven backend stop ---------------------


async def test_spawn_rpc_pre_launch_failure_cleans_the_reservation_once(
    theater_home, fake_tmux, tmp_path, monkeypatch
):
    """A failure before Spawner.launch is the RPC's reservation to clean."""
    repo = _init_repo(tmp_path / "repo")
    io = _RoutingIO()
    harness = _Harness()
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    d = await _daemon(io, harness, fake_tmux)

    def create_refuses(**kwargs):
        raise BadRequest("job table refuses")

    monkeypatch.setattr(d.jobs, "create", create_refuses)
    try:
        with pytest.raises(BadRequest, match="job table refuses"):
            await _spawn(d, _request(prompt="never launched", cwd=repo, worktree=True))
        participants = d.registry.list(include_dead=True)
        assert len(participants) == 1
        failed = participants[0]
        assert failed.status is Status.DEAD, "pre-launch failure cleans"
        assert not Path(failed.cwd).is_dir(), "the worktree is retired"
        assert d.store.get_runtime_binding(failed.id) is None, "no launch, no binding"
    finally:
        await d.aclose()


async def test_participant_kill_preserves_worktree_until_the_backend_stop_is_proven(
    theater_home, fake_tmux, tmp_path, rig, monkeypatch
):
    repo = _init_repo(tmp_path / "repo")
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = None
    refuse = [False]
    real_teardown = d.runtime_manager.teardown

    async def teardown_spy(participant_id, **kwargs):
        if refuse[0]:
            raise RuntimeError("teardown refused: backend cannot be stopped")
        return await real_teardown(participant_id, **kwargs)

    monkeypatch.setattr(d.runtime_manager, "teardown", teardown_spy)
    try:
        p = await _spawn(d, _request(prompt="in flight", cwd=repo, worktree=True))
        binding = d.store.get_runtime_binding(p.id)
        refuse[0] = True
        with pytest.raises(TheaterError, match="teardown could not be verified"):
            await participants_mod._kill(d, {"id": p.id})
        # The kill could not prove the backend stopped: the worktree and the
        # binding survive it — never retired on an unverified teardown.
        kept = d.registry.get(p.id)
        assert kept.status is not Status.DEAD, "no retirement without a proven stop"
        assert d.store.get_runtime_binding(p.id) is not None, "binding kept"
        assert Path(kept.cwd).is_dir(), "the worktree is not retired"
        assert _pid_alive(binding.backend_pid), "the backend was not signalled"

        # The reaper owns the retry: once the stop verifies, the preserved
        # retirement completes.
        refuse[0] = False
        await d._reap_once()
        assert d.registry.get(p.id).status is Status.DEAD
        assert d.store.get_runtime_binding(p.id) is None
        assert not Path(kept.cwd).is_dir(), "the worktree is retired after the retry"
        await _await_reaped(binding.backend_pid)
    finally:
        refuse[0] = False
        if p is not None and d.store.get_runtime_binding(p.id) is not None:
            with contextlib.suppress(Exception):
                await _teardown(d, p.id)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(d.store.get_runtime_binding(p.id).backend_pid, signal.SIGKILL)
        await d.aclose()


async def test_confirmed_exit_preserves_worktree_until_the_backend_stop_is_proven(
    theater_home, fake_tmux, tmp_path, rig, monkeypatch
):
    repo = _init_repo(tmp_path / "repo")
    d = await _daemon(rig.io, rig.harness, fake_tmux)
    p = None
    refuse = [False]
    real_teardown = d.runtime_manager.teardown

    async def teardown_spy(participant_id, **kwargs):
        if refuse[0]:
            raise RuntimeError("teardown refused: backend cannot be stopped")
        return await real_teardown(participant_id, **kwargs)

    monkeypatch.setattr(d.runtime_manager, "teardown", teardown_spy)
    try:
        p = await _spawn(d, _request(prompt="", cwd=repo, worktree=True))
        binding = d.store.get_runtime_binding(p.id)
        refuse[0] = True
        fake_tmux.remove_pane(p.tmux_pane)
        await d._reap_once()

        # Confirmed exit with an unverifiable backend stop: dead, but the
        # worktree and binding are preserved for the reaper's retry.
        exited = d.registry.get(p.id)
        assert exited.status is Status.DEAD
        assert d.store.get_runtime_binding(p.id) is not None, "binding kept"
        assert Path(exited.cwd).is_dir(), "the worktree is not retired"
        assert _pid_alive(binding.backend_pid), "the backend was not signalled"

        # The reaper sweep is the retry: once the stop verifies, the
        # retirement completes with self-exit branch policy.
        refuse[0] = False
        await d._reap_once()
        assert d.store.get_runtime_binding(p.id) is None
        assert not Path(exited.cwd).is_dir(), "the worktree is retired after the retry"
        await _await_reaped(binding.backend_pid)
    finally:
        refuse[0] = False
        if p is not None and d.store.get_runtime_binding(p.id) is not None:
            with contextlib.suppress(Exception):
                await _teardown(d, p.id)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(d.store.get_runtime_binding(p.id).backend_pid, signal.SIGKILL)
        await d.aclose()
