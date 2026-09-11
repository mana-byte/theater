"""Same-runtime live recovery: daemon-composed CodexRuntime, scripted backend.

The daemon composition exercises the generic recovery seam end to end, with
no manual reconnect call anywhere: the manager's bounded health monitor
detects a DISCONNECTED notification stream (a closed connection, or a
transport notification overflow surfaced as a disconnect) and the injected
recovery callback reconnects the exact persisted binding — one fresh
initialize, the exact thread/resume, the verified live backend reused, the
manifest's live source re-registered — so the exact buffered terminal
evidence completes the spawn job under the original generation and session.
No backend or UI relaunch, no prompt replay, no legacy fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from dataclasses import replace

from tests.test_codex_native_runtime_plugin import (
    ScriptedCodexConnection,
    ScriptedCodexServer,
    summary_turn_completed,
    thread_started,
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
from theater.harness.observation import TranscriptObserver
from theater.models import JobState

BIND_SLEEP_SNIPPET = """import socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind({path!r})
s.listen(1)
time.sleep(300)
"""

HARNESS_NAME = "codex"
EXACT_RESULT = "exact final answer"


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _await_reaped(pid: int | None, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"pid {pid} is still alive; the backend was not terminated")


async def _wait_until(predicate, *, timeout: float = 5.0, what: str) -> None:
    """Bounded wait for one condition; a hang detector, never a sleep."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.01)


class _Obs(TranscriptObserver):
    """A transcript-less observer: the live source is the only wiring."""

    has_transcript = False

    def find_transcript(self, *, cwd, session_id=None, after=None):  # pragma: no cover
        return None

    def session_id(self, transcript):  # pragma: no cover
        return None

    def parse(self, line, index, *, clip_text=True):  # pragma: no cover
        return []

    def is_idle_screen(self, capture):  # pragma: no cover
        return False


class _OverflowCodexConnection(ScriptedCodexConnection):
    """A scripted connection whose notification stream can overflow.

    The raise happens only after every buffered notification has been
    delivered (the close-marker semantics the real transport guarantees),
    so terminal evidence is drained first and the overflow surfaces as a
    disconnect.
    """

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


class _RoutingCodexIO(RuntimeIO):
    """The daemon's shared runtime I/O swapped for in-process routing.

    Every endpoint reaches its own scripted app-server; an optional
    per-endpoint gate holds one connect open (a blocked reconnect), and an
    overflow flag makes that endpoint's live connections drain-then-raise.
    """

    def __init__(self) -> None:
        self.servers: dict[str, ScriptedCodexServer] = {}
        self.connections: list[_OverflowCodexConnection] = []
        self.gates: dict[str, asyncio.Event] = {}
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
            await gate.wait()
        server = self.servers[endpoint]
        server.connect_count += 1
        connection = _OverflowCodexConnection(
            server, overflowing=lambda: endpoint in self.overflowing
        )
        self.connections.append(connection)
        return connection


class _CodexLiveHarness(Harness):
    """A runtime-capable harness whose manifest builds real CodexRuntimes."""

    name = HARNESS_NAME
    binary = HARNESS_NAME
    launch_parameter_support = LaunchParameterSupport(model=True, resume=True)
    #: A fork delivers its prompt through the control service, not the argv.
    resume_takes_prompt = True

    def __init__(self, io: _RoutingCodexIO) -> None:
        self.observer = _Obs()
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
            # The fake backend binds the private endpoint socket before
            # sleeping, the way the stock native backend measurably binds
            # only after exec: the production launch waits for that bind.
            snippet = BIND_SLEEP_SNIPPET.format(path=str(endpoint_to_path(context.endpoint)))
            # The pane UI would create the thread once launched; the
            # scripted backend broadcasts thread/started with the exact
            # working directory, which the runtime's NEW open waits for.
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


async def _compose(io: _RoutingCodexIO, harness: _CodexLiveHarness, fake_tmux) -> Daemon:
    """The real daemon composition: manager, shared I/O, controls, observer.

    The manifest is delivered through the harness registry exactly the way
    the lifecycle rig does it; the observer keeps this harness (live-source
    wiring), and the production spawn RPC drives the launch.
    """
    fake_tmux.visible_panes.clear()
    d = Daemon(harnesses={harness.name: harness})
    # ``Daemon.__init__`` re-installs the shipped harness registry, so the
    # test harness registers itself after construction (``clean_registry``
    # still restores the shipped set when the test ends).
    HARNESSES[harness.name] = harness
    d.runtime_io = io
    d.spawner.runtime_io = io
    await d.start()
    return d


async def _spawn(daemon: Daemon, *, prompt: str = "do the wave") -> str:
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


def _disconnect(io: _RoutingCodexIO, index: int) -> None:
    """Kill the daemon's notification stream on one scripted connection."""
    io.connections[index].closed = True


def _server(io: _RoutingCodexIO, pid: str) -> ScriptedCodexServer:
    server = io.servers[wiring_mod.native_endpoint(pid)]
    assert server is not None
    return server


async def _compose_and_spawn(fake_tmux, monkeypatch) -> tuple[_RoutingCodexIO, Daemon]:
    """The composed daemon with the monitor cadence patched to test speed."""
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", 0.05)
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", 0.05)
    io = _RoutingCodexIO()
    harness = _CodexLiveHarness(io)
    d = await _compose(io, harness, fake_tmux)
    return io, d


def _completed_evidence() -> object:
    return summary_turn_completed(
        turn_id="turn-1",
        items=[{"id": "i-final", "type": "agentMessage", "text": EXACT_RESULT}],
        items_view="summary",
    )


async def _teardown(daemon: Daemon, pid: str) -> None:
    """Verified-identity cleanup: terminate the spawned backend, if any."""
    with contextlib.suppress(Exception):
        await daemon.runtime_manager.teardown(pid, backend_generation=1)


# ---- automatic disconnect recovery --------------------------------------------


async def test_disconnected_stream_recovers_exact_session_and_completes_the_job(
    theater_home, fake_tmux, monkeypatch
) -> None:
    """Disconnected + backend alive: one fresh initialize, exact resume, exact job.

    The monitor — not any manual call — detects the dead notification
    stream and recovers the exact persisted generation/session; the backend
    and its UI pane are reused (never relaunched), no prompt is replayed,
    and the exact buffered terminal evidence completes the spawn job under
    the original generation and session.
    """
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        assert binding.backend_pid is not None
        backend_pid = binding.backend_pid
        session_id = binding.native_session_id
        assert session_id is not None
        server = _server(io, pid)
        first = d.runtime_manager.get(pid)
        assert isinstance(first, CodexRuntime)

        # The notification stream dies while the verified backend lives on.
        _disconnect(io, 0)
        await _wait_until(lambda: server.connect_count == 2, what="one automatic reconnect")

        # Exactly one fresh initialize beyond the spawn handshake, and the
        # exact thread/resume of the persisted session id.
        assert len(server.requested("initialize")) == 2
        resumes = server.requested("thread/resume")
        assert resumes[-1] == {"threadId": session_id}
        # No backend relaunch, no second UI, no prompt replay.
        assert _pid_alive(backend_pid), "the live backend is reused, never relaunched"
        assert len(fake_tmux.windows) == 1, "no second UI may be launched"
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
        server.push(_completed_evidence())
        await _wait_until(
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
            await _teardown(d, pid)
        await d.aclose()


# ---- overflow follows the same reconnect path ----------------------------------


async def test_notification_overflow_drains_evidence_then_recovers(
    theater_home, fake_tmux, monkeypatch
) -> None:
    """An overflow surfaces as a disconnect only after buffered evidence drains.

    The exact buffered terminal outcome is not lost, the same recovery path
    runs, and no turn/start replay happens.
    """
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        server = _server(io, pid)
        endpoint = wiring_mod.native_endpoint(pid)

        # Buffer the exact terminal outcome, then overflow the stream: the
        # transport drains the buffered notification first, then raises.
        server.push(_completed_evidence())
        io.overflowing.add(endpoint)
        await _wait_until(
            lambda: (
                d.store.get_job(pid) is not None and d.store.get_job(pid).state == JobState.DONE
            ),
            what="the buffered exact outcome completing the job",
        )
        await _wait_until(lambda: server.connect_count == 2, what="the overflow recovery reconnect")
        assert len(server.requested("turn/start")) == 1, "no prompt is ever replayed"
        assert server.requested("thread/resume")[-1] == {"threadId": session_id}
        job = d.store.get_job(pid)
        assert job.result == EXACT_RESULT, "no exact terminal outcome is missed"
    finally:
        if pid is not None:
            await _teardown(d, pid)
        await d.aclose()


# ---- stale persisted generation never recovers ---------------------------------


async def test_stale_persisted_generation_cannot_reconnect(
    theater_home, fake_tmux, monkeypatch
) -> None:
    """A monitor whose persisted generation was replaced recovers nothing.

    The persisted binding — not the registry — is the recovery authority:
    once the binding names a different generation, the (pid, generation 1)
    monitor is a bounded no-op that retries nothing and reconnects nothing.
    """
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        server = _server(io, pid)

        # A replacement generation now owns the persisted binding.
        d.store.upsert_runtime_binding(replace(binding, backend_generation=2))
        _disconnect(io, 0)
        await asyncio.sleep(0.35)  # past many patched poll/retry intervals
        assert server.connect_count == 1, "a stale generation never reconnects"
        assert d.runtime_manager.get(pid) is not None, "the live runtime is left untouched"

        # Restore the exact binding for the teardown cleanup.
        d.store.upsert_runtime_binding(binding)
    finally:
        if pid is not None:
            await _teardown(d, pid)
        await d.aclose()


# ---- one blocked reconnect never blocks another participant ---------------------


async def test_blocked_reconnect_for_one_participant_does_not_block_another(
    theater_home, fake_tmux, monkeypatch
) -> None:
    """A blocked recovery/open leaves the other participant fully responsive."""
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid1 = pid2 = None
    try:
        pid1 = await _spawn(d)
        pid2 = await _spawn(d)
        server1 = _server(io, pid1)
        server2 = _server(io, pid2)

        # Hold pid2's reconnect connect open; then disconnect both streams.
        gate = asyncio.Event()
        io.gates[wiring_mod.native_endpoint(pid2)] = gate
        _disconnect(io, 0)
        _disconnect(io, 1)

        await _wait_until(lambda: server1.connect_count == 2, what="p1's unblocked recovery")
        runtime1 = d.runtime_manager.get(pid1)
        assert runtime1 is not None
        snapshot1 = await runtime1.snapshot()
        assert snapshot1.health.value == "connected", "p1 recovered while p2 is blocked"

        # Runtime/control lookups stay responsive for both participants.
        assert d.runtime_manager.get(pid1) is not None
        runtime2 = d.runtime_manager.get(pid2)
        assert runtime2 is not None
        snapshot2 = await runtime2.snapshot()
        assert snapshot2.health.value == "disconnected", "p2's recovery is still blocked"
        assert server2.connect_count == 1

        gate.set()
        await _wait_until(lambda: server2.connect_count == 2, what="p2's recovery after the gate")
        runtime2 = d.runtime_manager.get(pid2)
        assert runtime2 is not None
        snapshot2 = await runtime2.snapshot()
        assert snapshot2.health.value == "connected", "p2 recovers once unblocked"
    finally:
        for pid in (pid1, pid2):
            if pid is not None:
                await _teardown(d, pid)
        await d.aclose()


# ---- close/teardown and daemon shutdown own the monitor ------------------------


async def test_disconnect_then_teardown_performs_no_post_teardown_reconnect(
    theater_home, fake_tmux, monkeypatch
) -> None:
    """Teardown cancels the monitor: no reconnect, and the backend terminates."""
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None and binding.backend_pid is not None
        server = _server(io, pid)

        _disconnect(io, 0)
        await d.runtime_manager.teardown(pid, backend_generation=1)
        await asyncio.sleep(0.35)  # past many patched poll/retry intervals
        assert server.connect_count == 1, "no reconnect may follow the teardown"
        assert d.runtime_manager._monitors == {}, "the owned monitor is cancelled"
        await _await_reaped(binding.backend_pid)
    finally:
        if pid is not None:
            with contextlib.suppress(Exception):
                await d.runtime_manager.teardown(pid, backend_generation=1)
        await d.aclose()


async def test_disconnect_then_daemon_close_performs_no_post_close_reconnect(
    theater_home, fake_tmux, monkeypatch
) -> None:
    """Daemon shutdown cancels the monitors and never terminates the backend."""
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None and binding.backend_pid is not None
        server = _server(io, pid)

        _disconnect(io, 0)
        await d.aclose()
        await asyncio.sleep(0.35)  # past many patched poll/retry intervals
        assert server.connect_count == 1, "no reconnect may follow daemon shutdown"
        assert d.runtime_manager._monitors == {}, "every owned monitor is cancelled"
        assert _pid_alive(binding.backend_pid), "the backend and its UI survive shutdown"
    finally:
        if pid is not None:
            with contextlib.suppress(Exception):
                # Reap the surviving backend the way the next daemon's
                # teardown would: verified identity, explicit generation.
                await d.runtime_manager.teardown(pid, backend_generation=1)
