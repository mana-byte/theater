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

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from tests.test_codex_native_runtime_plugin import (
    RequestFailure,
    ScriptedCodexConnection,
    ScriptedCodexServer,
    summary_turn_completed,
    thread_started,
    thread_status_changed,
)
from theater.daemon.harness_runtime import endpoint_to_path
from theater.daemon.harness_runtime import manager as manager_mod
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.rpc import spawning as spawning_rpc
from theater.daemon.runtime import recovery as recovery_mod
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
    ConnectionHealth,
    LiveChannelDeclaration,
    RuntimeCompatibility,
    RuntimeConnectionError,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeIO,
    RuntimeManifest,
    RuntimePlan,
    RuntimeRequestTimeout,
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


async def _compose_and_spawn(
    fake_tmux,
    monkeypatch,
    *,
    poll: float = 0.05,
    retry: float = 0.05,
) -> tuple[_RoutingCodexIO, Daemon]:
    """The composed daemon with the monitor cadence patched to test speed."""
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_POLL_SECONDS", poll)
    monkeypatch.setattr(manager_mod, "RUNTIME_RECOVERY_RETRY_SECONDS", retry)
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


async def test_ambiguous_prompt_start_invalidates_stale_idle_before_fifo_recovery(  # noqa: PLR0915
    theater_home, fake_tmux, monkeypatch
) -> None:
    """A lost turn/start acknowledgement cannot let stale IDLE dispatch FIFO.

    The actual Codex runtime synchronously invalidates its cached idle view,
    then the composed manager performs one same-generation reconnect.  A
    fresh active resume view and its exact terminal evidence remain unable to
    guess an uncorrelated prompt's turn; only a later fresh native IDLE can
    release the durable execution barrier and let the queued head start.
    """
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        assert binding.backend_pid is not None
        session_id = binding.native_session_id
        assert session_id is not None
        backend_pid = binding.backend_pid
        server = _server(io, pid)
        initial = d.runtime_manager.get(pid)
        assert isinstance(initial, CodexRuntime)

        # Establish a genuinely fresh idle baseline after the spawn prompt
        # completed.  The lost-ack control below must not reuse this view.
        server.push(_completed_evidence())
        server.push(thread_status_changed("idle", thread_id=session_id))
        await _wait_until(
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

        # Hold the automatic reconnect while an acknowledgement-lost
        # turn/start leaves only an ambiguous native mutation.  The queued
        # prompt is created through the real ControlService, not a helper.
        endpoint = wiring_mod.native_endpoint(pid)
        reconnect_gate = asyncio.Event()
        io.gates[endpoint] = reconnect_gate
        server.fail("turn/start", RuntimeRequestTimeout())
        first = await d.controls.send(pid, caller_id="cli", prompt="unknown first")
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
            "do the wave",
            "unknown first",
        ]
        assert fake_tmux.sent == [], "an ambiguous native send never falls back to tmux"

        # The automatic monitor reconnects exactly once to the persisted
        # session.  Its fresh view is ACTIVE with no terminal result, so the
        # barrier and the FIFO head must remain blocked.
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
        assert server.requested("thread/resume")[-1] == {"threadId": session_id}
        assert _pid_alive(backend_pid), "recovery reuses the verified backend"
        assert len(fake_tmux.windows) == 1, "recovery launches no replacement UI"
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the wave",
            "unknown first",
        ]
        assert d.store.has_execution_barrier(pid)
        assert d.store.queued_control_operation_count(pid) == 1

        # The recovered live source persists exact terminal evidence before
        # any job mutation.  The original UNKNOWN receipt carried no turn
        # identity, so exact attribution correctly refuses to guess that this
        # terminal turn belongs to it; the barrier stays active while status
        # remains ACTIVE.
        server.push(
            summary_turn_completed(
                thread_id=session_id,
                turn_id="turn-ambiguous",
                items_view="summary",
                items=[{"id": "i-ambiguous", "type": "agentMessage", "text": "exact"}],
            )
        )
        await _wait_until(
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
        await _wait_until(
            lambda: len(server.requested("turn/start")) == 3,
            what="the scheduled FIFO dispatch after authoritative idle",
        )
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the wave",
            "unknown first",
            "must stay queued",
        ]
        assert d.store.has_execution_barrier(pid) is False
        assert d.store.get_job(first.handle).state == JobState.RUNNING
        assert d.store.get_job(queued.handle).state == JobState.RUNNING
        assert fake_tmux.sent == []
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
    gate = asyncio.Event()
    try:
        pid1 = await _spawn(d)
        pid2 = await _spawn(d)
        server1 = _server(io, pid1)
        server2 = _server(io, pid2)
        initial2 = d.runtime_manager.get(pid2)
        assert isinstance(initial2, CodexRuntime)
        p2_turn_starts = list(server2.requested("turn/start"))

        # Hold pid2's reconnect connect open; then disconnect both streams.
        endpoint2 = wiring_mod.native_endpoint(pid2)
        p2_connect_entered = io.connect_entered.setdefault(endpoint2, asyncio.Event())
        io.gates[endpoint2] = gate
        _disconnect(io, 0)
        _disconnect(io, 1)

        await _wait_until(
            p2_connect_entered.is_set,
            what="p2's reconnect entering the blocked connect",
        )
        # ``reconnect`` installs this exact replacement before its
        # open_session reaches the gated connect.  Its safe non-connected
        # phase must remain UNKNOWN and must not deliver another prompt.
        runtime2 = d.runtime_manager.get(pid2)
        assert isinstance(runtime2, CodexRuntime)
        assert runtime2 is not initial2
        snapshot2 = await runtime2.snapshot()
        assert snapshot2.health in (ConnectionHealth.UNOPENED, ConnectionHealth.DISCONNECTED)
        assert snapshot2.execution_state is RuntimeExecutionState.UNKNOWN
        assert server2.requested("turn/start") == p2_turn_starts
        assert server2.connect_count == 1

        await _wait_until(lambda: server1.connect_count == 2, what="p1's unblocked recovery")
        runtime1 = d.runtime_manager.get(pid1)
        assert runtime1 is not None
        snapshot1 = await runtime1.snapshot()
        assert snapshot1.health.value == "connected", "p1 recovered while p2 is blocked"

        # Runtime/control lookups stay responsive for both participants.
        assert d.runtime_manager.get(pid1) is not None
        assert d.runtime_manager.get(pid2) is runtime2
        assert server2.connect_count == 1

        gate.set()
        await _wait_until(lambda: server2.connect_count == 2, what="p2's recovery after the gate")
        runtime2 = d.runtime_manager.get(pid2)
        assert runtime2 is not None
        snapshot2 = await runtime2.snapshot()
        assert snapshot2.health.value == "connected", "p2 recovers once unblocked"
    finally:
        gate.set()
        for pid in (pid1, pid2):
            if pid is not None:
                await _teardown(d, pid)
        await d.aclose()
        assert d.runtime_manager._monitors == {}
        assert d.controls.owned_tasks == ()


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


def _fake_create(runtime):
    async def create():
        return runtime

    return create


def _register_gen2(daemon: Daemon, participant_id: str, runtime: FakeRuntime) -> None:
    """Register the replacement generation's live source, as its lifecycle does."""
    channel = _CodexLiveHarness(_RoutingCodexIO())._manifest().channel
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
    theater_home, fake_tmux, monkeypatch
) -> None:
    """The gated open race: a stale completion returns False, registers nothing.

    While the generation-1 recovery awaits open_session, generation 2
    replaces the persisted binding and the manager's runtime. The stale
    generation-1 completion must return False without registering its
    source — the generation-2 registration stays current, so no
    cross-generation evidence attribution is possible.
    """
    # A quiet monitor: this regression drives the recovery function directly
    # so the race is deterministic; the monitor path is proven elsewhere.
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch, poll=3600.0, retry=0.05)
    pid = None
    candidate = None
    recovery_task = None
    gate = asyncio.Event()
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        first = d.runtime_manager.get(pid)
        assert first is not None

        # Hold the recovery candidate's open_session at its connect.
        io.gates[wiring_mod.native_endpoint(pid)] = gate
        recovery_task = asyncio.create_task(recovery_mod.recover_live_runtime(d, pid, 1))
        await _wait_until(
            lambda: d.runtime_manager.get(pid) is not first, what="the candidate installing"
        )
        candidate = d.runtime_manager.get(pid)
        await asyncio.sleep(0.05)
        assert not recovery_task.done(), "the recovery is parked at the gated open"

        # Generation 2 replaces the participant while the recovery awaits:
        # teardown the generation-1 backend/runtime, bump the persisted
        # binding, install the generation-2 runtime, and register its
        # source exactly as the replacement generation's lifecycle does.
        await d.runtime_manager.teardown(pid, backend_generation=1)
        d.store.upsert_runtime_binding(
            replace(binding, backend_generation=2, native_session_id="gen-2-session")
        )
        gen2 = _gen2_runtime(pid)
        await d.runtime_manager.get_or_create(
            pid,
            backend_generation=2,
            create=_fake_create(gen2),
        )
        _register_gen2(d, pid, gen2)
        registration_before = d.observer.live.registration_for(pid)
        assert registration_before is not None
        assert registration_before.backend_generation == 2

        gate.set()
        recovered = await asyncio.wait_for(recovery_task, 5.0)

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
                await asyncio.wait_for(recovery_task, 5.0)
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
    theater_home, fake_tmux, monkeypatch
) -> None:
    """A failed session open is discarded in place and retried, bounded.

    Without the in-place discard, the failed candidate would read CONNECTED
    and suppress the health monitor forever. With it, each failed attempt
    retries on the bounded cadence and the eventual success registers the
    exact runtime/session — no relaunch, no replay.
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
        assert server.connect_count == 1

        # Every re-adoptation of the exact session fails until the test
        # clears the failure: the candidate installs, then open fails.
        server.fail("thread/resume", RequestFailure("scripted resume refused"))
        _disconnect(io, 0)

        # The monitor retries: at least two failed candidates connect
        # beyond the spawn handshake before the failure is cleared.
        await _wait_until(lambda: server.connect_count >= 3, what="the bounded retry cadence")
        server.failures.pop("thread/resume", None)

        def _recovered() -> bool:
            registration = d.observer.live.registration_for(pid)
            runtime = d.runtime_manager.get(pid)
            return (
                registration is not None
                and runtime is not None
                and registration.live_source is runtime.live_source()
            )

        await _wait_until(_recovered, what="the eventual registration")
        runtime = d.runtime_manager.get(pid)
        assert isinstance(runtime, CodexRuntime)
        snapshot = await runtime.snapshot()
        assert snapshot.health.value == "connected"
        assert snapshot.native_session_id == session_id
        assert snapshot.backend_generation == 1
        # No relaunch, no second UI, no prompt replay across all attempts.
        assert _pid_alive(binding.backend_pid)
        assert len(fake_tmux.windows) == 1
        assert len(server.requested("turn/start")) == 1

        # The eventually-registered runtime completes the exact job.
        server.push(_completed_evidence())

        def _job_done() -> bool:
            job = d.store.get_job(pid)
            return job is not None and job.state == JobState.DONE

        await _wait_until(_job_done, what="the job completing after the retried recovery")
        assert d.store.get_job(pid).result == EXACT_RESULT
    finally:
        if pid is not None:
            await _teardown(d, pid)
        await d.aclose()


async def test_registration_failure_is_retryable_and_never_closes_a_successor(  # noqa: PLR0915
    theater_home, fake_tmux, monkeypatch
) -> None:
    """A failed registration discards the candidate in place, never a successor.

    Recovery never closes the manager's runtime and never unregisters live
    wiring: a failed registration is discarded by exact runtime identity,
    stays fail-closed in place (still the manager's current runtime, its
    snapshot disconnected), and retries on the bounded cadence until it
    registers the exact replacement.
    """
    # A wide retry gap so the discarded-in-place state is sampled
    # deterministically between the failed attempt and the next one.
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch, retry=0.5)
    pid = None
    fail_registration = False
    retry_connect_gate = asyncio.Event()
    failed_candidate: CodexRuntime | None = None
    failed_candidate_was_current = False
    failed_candidate_discarded = asyncio.Event()
    try:
        pid = await _spawn(d)
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

        # Observe the exact candidate's disconnect, not a later manager
        # lookup that can already be a retry candidate.  This stays entirely
        # inside the test double; production recovery cadence is unchanged.
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
                # A retry cannot replace the exact discarded candidate before
                # the assertion below.  It resumes only after the test has
                # released the failure and this gate.
                io.gates[endpoint] = retry_connect_gate
                raise RuntimeError("scripted registration failure")
            return real_register(registration)

        fail_registration = True
        monkeypatch.setattr(d.observer.live, "register", register_spy)

        _disconnect(io, 0)
        await _wait_until(lambda: registration_failures >= 1, what="the failed registration")
        await asyncio.wait_for(failed_candidate_discarded.wait(), timeout=5.0)

        # The candidate that failed registration is still the manager's
        # current runtime — discarded in place, fail-closed, and now
        # reading DISCONNECTED so the monitor retries.
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

        await _wait_until(
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
            await _teardown(d, pid)
        await d.aclose()
