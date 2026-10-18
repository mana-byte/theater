"""Native-runtime lifecycle rig: routing I/O, recording runtimes and a test harness."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path

from tests.support.backend_snippets import BIND_SLEEP_SNIPPET, SLEEP_SNIPPET
from tests.support.fake_runtime import (
    FakeRuntime,
    FakeRuntimeConnection,
    FakeRuntimeState,
)
from tests.support.runtime_processes import (
    NoTranscriptObserver,
)
from theater.daemon.harness_runtime import endpoint_to_path
from theater.daemon.server import Daemon
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
    RuntimeContext,
    RuntimeIO,
    RuntimeManifest,
    RuntimePlan,
    SessionOpenMode,
)


class RoutingIO(RuntimeIO):
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


class RecordingRuntime(FakeRuntime):
    """The fake runtime with order-of-operations evidence."""

    def __init__(self, context: RuntimeContext, events: list) -> None:
        super().__init__(context)
        self._events = events
        self.context_harness = "native-fixture"

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


class FailingOpenRuntime(FakeRuntime):
    """Fails session discovery, simulating a dead private endpoint."""

    async def open_session(self, *, mode, native_session_id=None):
        raise ConnectionError("fake backend refused the session open")


class StalledOpenRuntime(FakeRuntime):
    """Session discovery never resolves, simulating a stalled startup."""

    async def open_session(self, *, mode, native_session_id=None):
        if mode is SessionOpenMode.NEW:
            await asyncio.Event().wait()
        return await super().open_session(mode=mode, native_session_id=native_session_id)


class NativeFixtureHarness(Harness):
    """A runtime-capable test harness; ``plan_launch`` is the legacy path."""

    name = "native-fixture"
    binary = "native-fixture"
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
        self.observer = NoTranscriptObserver()
        self.events: list = []
        #: Store handle for order assertions; wired by ``native_daemon``.
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
                policy="fixture-verified",
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
                return FailingOpenRuntime(context)
            if harness._open_stalls:
                return StalledOpenRuntime(context)
            return RecordingRuntime(context, harness.events)

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
        resume: str | None = None,
        mcp_servers=(),
    ) -> LaunchPlan:
        del resume
        self.events.append(("legacy_plan", prompt))
        return LaunchPlan(argv=[self.binary, "--prompt", prompt])


def spawn_request(**kwargs) -> SpawnRequest:
    kwargs.setdefault("harness", "native-fixture")
    kwargs.setdefault("prompt", "do the task")
    kwargs.setdefault("cwd", "/tmp")
    kwargs.setdefault("approval", "manual")
    return SpawnRequest(**kwargs)


async def spawn_native(daemon: Daemon, req: SpawnRequest):
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
            "wiring": req.wiring.value,
        },
    )
    return daemon.registry.get(result["handle"])


async def native_daemon(io: RoutingIO, harness: NativeFixtureHarness, terminal_provider) -> Daemon:
    # The fake tmux pre-declares %1-%3 as live "vibe" panes; the first spawned
    # window also draws pane id %1, and the pane identity check would read the
    # pre-declared row instead of ours. These tests track only their own panes.
    terminal_provider.terminals.clear()
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


def spy_on_backend_launch(daemon: Daemon, captured: dict) -> None:
    """Record the real backend pid at launch, before any cleanup can delete it."""
    real_launch = daemon.runtime_manager.launch_backend

    async def launch_spy(participant_id, **kwargs):
        identity = await real_launch(participant_id, **kwargs)
        captured["pid"] = identity.pid
        return identity

    daemon.runtime_manager.launch_backend = launch_spy


async def teardown_backend(daemon: Daemon, pid: str) -> None:
    with contextlib.suppress(Exception):
        await daemon.runtime_manager.teardown(pid, backend_generation=1)


def init_repo(path: Path) -> str:
    """A real git repo with one commit, for worktree-retirement assertions."""
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return str(path)
