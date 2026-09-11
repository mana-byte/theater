"""Same-runtime live recovery: daemon-composed CodexRuntime, scripted backend."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from dataclasses import replace

import pytest

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
    RuntimeNotification,
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


class _RoutingCodexIO(RuntimeIO):
    """The daemon's shared runtime I/O swapped for in-process routing."""

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


async def _compose(io: _RoutingCodexIO, harness: _CodexLiveHarness, fake_tmux) -> Daemon:
    """The real daemon composition: manager, shared I/O, controls, observer."""
    fake_tmux.visible_panes.clear()
    d = Daemon(harnesses={harness.name: harness})
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
    """Disconnected + backend alive: one fresh initialize, exact resume, exact job."""
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
        assert resumes[-1] == {
            "threadId": session_id,
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
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
    """A lost turn/start acknowledgement cannot let stale IDLE dispatch FIFO."""
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

        # Hold the automatic reconnect while an acknowledgement-lost turn/start leaves only an
        # ambiguous native mutation.
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
        assert _pid_alive(backend_pid), "recovery reuses the verified backend"
        assert len(fake_tmux.windows) == 1, "recovery launches no replacement UI"
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the wave",
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


async def test_cancelled_post_write_prompt_stays_unknown_until_fresh_reconciliation(  # noqa: PLR0915
    theater_home, fake_tmux, monkeypatch
) -> None:
    """A startup-shaped cancellation after turn/start writes cannot reuse IDLE."""
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    reconnect_gate = asyncio.Event()
    write_gate = asyncio.Event()
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        assert binding.backend_pid is not None
        backend_pid = binding.backend_pid
        window_count = len(fake_tmux.windows)
        server = _server(io, pid)
        initial = d.runtime_manager.get(pid)
        assert isinstance(initial, CodexRuntime)

        # The initial Theater prompt completes and establishes a genuinely
        # authoritative idle baseline that the cancelled write must discard.
        server.push(_completed_evidence())
        server.push(thread_status_changed("idle", thread_id=session_id))
        await _wait_until(
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
        cancelled = asyncio.create_task(
            asyncio.wait_for(
                d.controls.send(pid, caller_id="cli", prompt="cancelled after wire write"),
                timeout=0.05,
            )
        )
        await _wait_until(
            lambda: server.request_entered.get("turn/start", asyncio.Event()).is_set(),
            what="the blocked physical turn/start write",
        )
        try:
            await cancelled
            raise AssertionError("the startup-shaped cancellation must reach the service send")
        except TimeoutError:
            pass

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

        queued = await d.controls.queue_followup(pid, caller_id="cli", prompt="must wait")
        await asyncio.sleep(0.08)
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the wave",
            "cancelled after wire write",
        ]
        assert d.store.get_job(queued.handle).state == JobState.RUNNING
        assert d.store.has_execution_barrier(pid)
        assert fake_tmux.sent == [], "a cancelled native write never falls back to tmux"

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
        assert _pid_alive(backend_pid), "recovery reuses the verified backend"
        assert len(fake_tmux.windows) == window_count, "recovery launches no replacement UI"
        assert len(server.requested("turn/start")) == 2
        assert d.store.has_execution_barrier(pid)

        # Only later exact IDLE releases the unresolved execution boundary;
        # daemon-owned maintenance then starts the queued prompt once.
        server.push(thread_status_changed("idle", thread_id=session_id))
        await _wait_until(
            lambda: len(server.requested("turn/start")) == 3,
            what="the queued prompt after fresh authoritative idle",
        )
        assert [request["input"][0]["text"] for request in server.requested("turn/start")] == [
            "do the wave",
            "cancelled after wire write",
            "must wait",
        ]
    finally:
        write_gate.set()
        reconnect_gate.set()
        if pid is not None:
            await _teardown(d, pid)
        await d.aclose()


async def test_reconnect_recovers_old_theater_turn_beyond_later_native_ui_turns(
    theater_home, fake_tmux, monkeypatch
) -> None:
    """Automatic recovery crosses page 64 and a transient failure, then advances FIFO."""
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    try:
        pid = await _spawn(d)
        binding = d.store.get_runtime_binding(pid)
        assert binding is not None
        session_id = binding.native_session_id
        assert session_id is not None
        server = _server(io, pid)
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
        _disconnect(io, 0)
        await _wait_until(
            lambda: server.connect_count == 2,
            what="the automatic same-session reconnect",
        )
        await _wait_until(
            lambda: (
                d.store.get_job(pid) is not None and d.store.get_job(pid).state == JobState.DONE
            ),
            what="the old exact Theater turn completing through paged recovery",
            timeout=20.0,
        )
        await _wait_until(
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
        assert fake_tmux.sent == []
    finally:
        if pid is not None:
            await _teardown(d, pid)
        await d.aclose()


@pytest.mark.parametrize("case", ["old_ui", "old_finished_job", "missed_current"])
async def test_history_interruption_only_cancels_its_original_followups(
    theater_home, fake_tmux, monkeypatch, case
) -> None:
    """Backfill preserves new intent while a missed current interruption cancels its cohort."""
    io, d = await _compose_and_spawn(fake_tmux, monkeypatch)
    pid = None
    history_gate = asyncio.Event()
    try:
        pid = await _spawn(d)
        session = d.store.get_runtime_binding(pid).native_session_id
        server = _server(io, pid)
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
        _disconnect(io, 0)
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
        await _wait_until(
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
            await _wait_until(
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
            await _wait_until(
                lambda: d.store.get_job(after.handle).state == JobState.KILLED,
                what="a current native-UI interruption cancelling the pending followup",
            )
            assert d.store.get_job(after.handle).error_code == "interrupted"
        assert server.connect_count == 2
        assert fake_tmux.sent == []
    finally:
        history_gate.set()
        if pid is not None:
            await _teardown(d, pid)
        await d.aclose()


# ---- overflow follows the same reconnect path ----------------------------------


async def test_notification_overflow_drains_evidence_then_recovers(
    theater_home, fake_tmux, monkeypatch
) -> None:
    """An overflow surfaces as a disconnect only after buffered evidence drains."""
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
        assert server.requested("thread/resume")[-1] == {
            "threadId": session_id,
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
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
    """A monitor whose persisted generation was replaced recovers nothing."""
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
    """The gated open race: a stale completion returns False, registers nothing."""
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
    """A failed session open is discarded in place and retried, bounded."""
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
    """A failed registration discards the candidate in place, never a successor."""
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

        _disconnect(io, 0)
        await _wait_until(lambda: registration_failures >= 1, what="the failed registration")
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
