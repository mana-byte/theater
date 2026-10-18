"""Composed-daemon rig for Codex live-recovery tests: routing I/O and a scripted backend."""

from __future__ import annotations

import asyncio
import sys

from tests.support.backend_snippets import BIND_SLEEP_SNIPPET
from tests.support.codex_server import (
    ScriptedCodexConnection,
    ScriptedCodexServer,
    summary_turn_completed,
    thread_started,
)
from tests.support.runtime_processes import (
    NoTranscriptObserver,
)
from theater.daemon.harness_runtime import endpoint_to_path
from theater.daemon.harness_runtime import manager as manager_mod
from theater.daemon.rpc import spawning as spawning_rpc
from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.server import Daemon
from theater.harness import HARNESSES, Harness
from theater.harness.builtin.plugins.codex.runtime import CodexRuntime
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
    RuntimeIO,
    RuntimeManifest,
    RuntimePlan,
)

HARNESS_NAME = "codex"


EXACT_RESULT = "exact final answer"


class OverflowCodexConnection(ScriptedCodexConnection):
    """A scripted connection whose notification stream can overflow."""

    def __init__(self, server: ScriptedCodexServer, overflowing) -> None:
        super().__init__(server)
        self._overflowing = overflowing

    async def notifications(self):
        while not self.closed:
            if self._server.pending:
                yield self._server.pending.pop(0)
                continue
            if self._overflowing():
                raise RuntimeConnectionError("scripted notification overflow")
            await asyncio.sleep(0.005)


class RoutingCodexIO(RuntimeIO):
    """The daemon's shared runtime I/O swapped for in-process routing."""

    def __init__(self) -> None:
        self.servers: dict[str, ScriptedCodexServer] = {}
        self.connections: list[OverflowCodexConnection] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.connect_entered: dict[str, asyncio.Event] = {}
        self.overflowing: set[str] = set()

    def server_for(self, endpoint: str) -> ScriptedCodexServer:
        """One scripted app-server per endpoint, created at plan time."""
        server = self.servers.get(endpoint)
        if server is None:
            server = ScriptedCodexServer()
            self.servers[endpoint] = server
        return server

    async def connect(self, endpoint: str, *, timeout: float):
        del timeout
        gate = self.gates.get(endpoint)
        if gate is not None:
            self.connect_entered.setdefault(endpoint, asyncio.Event()).set()
            await gate.wait()
        server = self.servers[endpoint]
        server.connect_count += 1
        connection = OverflowCodexConnection(
            server, overflowing=lambda: endpoint in self.overflowing
        )
        self.connections.append(connection)
        return connection


class CodexLiveHarness(Harness):
    """A runtime-capable harness whose manifest builds real CodexRuntimes."""

    name = HARNESS_NAME
    binary = sys.executable
    launch_parameter_support = LaunchParameterSupport(model=True, resume=True)
    #: A fork delivers its prompt through the control service, not the argv.
    resume_takes_prompt = True

    def __init__(self, io: RoutingCodexIO) -> None:
        self.observer = NoTranscriptObserver()
        self.io = io
        self.runtime = self._manifest()

    def _manifest(self) -> RuntimeManifest:
        harness = self

        def probe(context) -> RuntimeCompatibility:
            del context
            return RuntimeCompatibility(
                supported=True, policy="scripted-verified", native_version="0.154.0"
            )

        def plan(context) -> RuntimePlan:
            server = harness.io.server_for(context.endpoint)
            snippet = BIND_SLEEP_SNIPPET.format(path=str(endpoint_to_path(context.endpoint)))
            server.push(thread_started(cwd=context.cwd))
            return RuntimePlan(
                backend=LaunchPlan(argv=[sys.executable, "-c", snippet]),
                endpoint=context.endpoint,
            )

        def factory(context):
            return CodexRuntime(context)

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
        config_path,
        approval: str,
        model=None,
        mcp_servers=(),
    ):
        del participant_id, prompt, config_path, approval, model, mcp_servers
        raise AssertionError("a native spawn never takes the legacy launch path")


async def compose_daemon(
    io: RoutingCodexIO, harness: CodexLiveHarness, terminal_provider
) -> Daemon:
    """The real daemon composition: manager, shared I/O, controls, observer."""
    terminal_provider.terminals.clear()
    d = Daemon(harnesses={harness.name: harness})
    HARNESSES[harness.name] = harness
    d.runtime_io = io
    d.spawner.runtime_io = io
    await d.start()
    return d


async def spawn_codex(daemon: Daemon, *, prompt: str = "do the task") -> str:
    """Spawn through the actual spawn RPC, with explicit native wiring."""
    result = await spawning_rpc._spawn(
        daemon,
        {
            "harness": HARNESS_NAME,
            "prompt": prompt,
            "cwd": "/tmp",
            "approval": "manual",
            "parent_id": None,
            "tmux_session": None,
            "window_name": None,
            "background": False,
            "worktree": None,
            "base_branch": None,
            "model": None,
            "reasoning_effort": None,
            "resume": None,
            "name": None,
            "description": None,
            "wiring": "native",
        },
    )
    return result["handle"]


def disconnect_stream(io: RoutingCodexIO, index: int) -> None:
    """Kill the daemon's notification stream on one scripted connection."""
    io.connections[index].closed = True


def scripted_server(io: RoutingCodexIO, pid: str) -> ScriptedCodexServer:
    server = io.servers[wiring_mod.native_endpoint(pid)]
    assert server is not None
    return server


async def compose_and_spawn(
    terminal_provider,
    monkeypatch,
    *,
    poll: float = 0.05,
    retry: float = 0.05,
) -> tuple[RoutingCodexIO, Daemon]:
    """The composed daemon with the monitor cadence patched to test speed."""
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", poll)
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", retry)
    io = RoutingCodexIO()
    harness = CodexLiveHarness(io)
    d = await compose_daemon(io, harness, terminal_provider)
    return io, d


def completed_evidence() -> object:
    return summary_turn_completed(
        turn_id="turn-1",
        items=[{"id": "i-final", "type": "agentMessage", "text": EXACT_RESULT}],
        items_view="summary",
    )
