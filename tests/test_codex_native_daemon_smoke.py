"""Wave 5 release smoke: the production daemon path against the real Codex release.

One chain, one live backend, one native UI, one prompt — every Theatre seam in
between is the real production composition, not a fake or a rig:

* the ``Daemon`` composition root (store, registry, spawner, runtime manager,
  WebSocket runtime I/O, control service, observer with its live hub) runs
  in-process on an isolated temporary ``THEATER_HOME`` — never the live
  orchestration daemon or its database;
* tmux is real and private: the daemon's tmux calls reach a throwaway server
  whose socket root belongs to this test alone (``TMUX_TMPDIR``), never the
  server hosting this session;
* the backend is the unmodified installed ``codex-cli 0.154.0`` app-server
  over its real Unix WebSocket endpoint, detached and owned by the daemon;
* the UI is the stock promptless native TUI (``codex --remote unix://...``)
  attached to the same backend, in a pane the daemon created;
* the spawn goes through the real ``spawn`` RPC with explicit
  ``wiring="native"`` — automatic selection stays disabled (the Wave 5 gate
  constant is asserted, not flipped);
* the initial prompt is dispatched exactly once through ``ControlService``
  onto the spawn job, the scripted model turn is held mid-flight, the daemon
  is closed (disconnect-only), a fresh daemon over the same home reconciles
  to the exact same backend/session with no second UI and no prompt replay,
  and the released turn finishes the exact job from native terminal evidence
  that is provably persisted *before* the job becomes done;
* teardown goes through the real ``participant.kill`` RPC and proves the
  backend is terminated before the worktree is retired.

Model answers come from the existing mock Responses server
(``tests/fixtures/codex_native_ui_bootstrap/``) on loopback, so the smoke
depends on no external network and no model billing. The transcript search
root of the shipped codex harness is rebound to the isolated ``CODEX_HOME``
via the production-provided ``manifest_for_root`` seam (``tests/shipped.py``)
so observation stays inside the test's private world too.

Opt-in and deterministic: ordinary pytest skips it with an explicit reason,
and a skipped run is not release evidence. Run it for real with:

    THEATER_CODEX_NATIVE_DAEMON_SMOKE=1 \\
        uv run --frozen pytest tests/test_codex_native_daemon_smoke.py -v

The launch-path readiness correction this smoke forced (Wave 5): the NEW-mode
native launch waits for the backend's private Unix endpoint to accept
connections (``wait_for_unix_endpoint``, a reachability probe that never
speaks the native protocol) after the verified pid/start identity is persisted
(``STARTED``) and before any runtime connection — ``frontend_plan`` /
``open_session`` — can begin, so the stock app-server's bind (~40-60 ms after
exec) can no longer race the connect into ENOENT and a healthy-backend
SIGTERM. The probe's budget is the same single 30-second startup deadline —
no separate readiness policy — and the existing outer ``asyncio.wait_for``
still bounds the whole pre-dispatch sequence; a readiness failure follows the
ordinary pre-dispatch cleanup.

Wave 5 latency evidence (test-only instrumentation on this run's instances):
the smoke prints two monotonic-clock measurements in ``-s`` output —
``native_event_to_daemon_ms`` runs from the Codex runtime's handling/queueing
of the exact terminal outcome (a wrapper on the runtime's
``_record_turn_outcome``, never the model gate release) to the daemon's
durable, visible completion of the exact job (stamped after the real
``jobs.finish`` returns, with the terminal evidence already persisted).
``daemon_to_regie_ms`` runs from the daemon's ``job.finished`` bus publication
to the real ``PollingController`` observing that exact event on the configured
``bus_interval`` cadence, primed before the turn release and driven through a
minimal in-process ``bus.tail`` adapter — a second real socket process adds
nothing to this measurement. The régie numbers are polling evidence only:
no push path is claimed, and ``regie_bus_interval_ms`` is printed with them.

Every resource is private to the run and cleaned up even on failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest
from shipped import CodexHarness

from theater.config import RegieSection
from theater.daemon.rpc import participants as participants_rpc
from theater.daemon.rpc import spawning as spawning_rpc
from theater.daemon.rpc import usage as usage_rpc
from theater.daemon.runtime import wiring as wiring_mod
from theater.daemon.server import Daemon
from theater.harness import HARNESSES
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import JobState, Status
from theater.provenance import TranscriptProvenance
from theater.regie.controllers.polling import PollingController

pytestmark = pytest.mark.tmux

SMOKE_ENV_VAR = "THEATER_CODEX_NATIVE_DAEMON_SMOKE"
EXPECTED_CODEX_VERSION = "codex-cli 0.154.0"
PROMPT = "Theater codex native daemon smoke: produce the scripted reply."
FINAL_MESSAGE = "Theater codex native daemon smoke: scripted model reply."

#: How long the held model response may stay held before the mock serves it
#: anyway (a bounded safety valve, not a test behaviour).
HOLD_TIMEOUT_SECONDS = 120.0

#: Bounded waits below are hang detectors, not timing assertions.
POLL_INTERVAL_SECONDS = 0.05
MODEL_REQUEST_DEADLINE_SECONDS = 60.0
JOB_DEADLINE_SECONDS = 90.0
REAP_DEADLINE_SECONDS = 15.0

#: Bounded eventual observation of the exact job.finished event by the real
#: régie polling controller — a generous hang detector, never a timing
#: threshold on the measured latency.
REGIE_OBSERVATION_DEADLINE_SECONDS = 90.0

TESTS_DIR = Path(__file__).parent
_FIXTURES = TESTS_DIR / "fixtures" / "codex_native_ui_bootstrap"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mock_responses = _load_module("codex_native_daemon_smoke_mock", _FIXTURES / "mock_responses.py")
codex_env = _load_module("codex_native_daemon_smoke_env", _FIXTURES / "codex_env.py")


# ---------------------------------------------------------------------------
# The private world
# ---------------------------------------------------------------------------


@dataclass
class SmokeWorld:
    """Every private resource one smoke run owns."""

    home: Path
    root: Path
    repo: Path
    codex_home: Path
    tmux_root: Path
    sessions_root: Path
    mock: object
    gate: threading.Event
    daemons: list = field(default_factory=list)
    backend_pids: list[int] = field(default_factory=list)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _await_pid_gone(pid: int, *, timeout: float, what: str) -> None:
    """Deadline-bounded liveness poll; never a blind fixed sleep."""
    deadline = time.monotonic() + timeout
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(POLL_INTERVAL_SECONDS)


async def _await_until(predicate, *, timeout: float, what: str) -> None:
    """Deadline-bounded async wait, polling without blocking the loop.

    ``SETTLED`` only proves the app-server accepted the turn; the model
    request to the mock races ahead of it, so readiness facts like that are
    awaited, never assumed.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _scripted_mock() -> tuple[object, threading.Event]:
    """The existing bootstrap-proof mock, with a gate on the turn's response.

    Only the scripted model stream for the initial prompt is held; the TUI's
    structured title-generation requests keep receiving filler. Releasing the
    gate lets the held turn complete deterministically.
    """
    final_stream = mock_responses.final_assistant_message_response(FINAL_MESSAGE)
    mock = mock_responses.MockResponsesServer([final_stream], marker=PROMPT)
    gate = threading.Event()
    real_next_stream = mock.next_stream

    def gated_next_stream(path, body_text=None, *, structured=False):
        if mock.request_matches(body_text, structured=structured):
            gate.wait(timeout=HOLD_TIMEOUT_SECONDS)
        return real_next_stream(path, body_text, structured=structured)

    mock.next_stream = gated_next_stream
    return mock, gate


def _tmux(world: SmokeWorld, *args: str) -> subprocess.CompletedProcess:
    """Talk to this test's private tmux server by its own socket root."""
    env = {**os.environ, "TMUX_TMPDIR": str(world.tmux_root)}
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False, env=env)


def _ui_panes(world: SmokeWorld) -> list[tuple[str, str]]:
    """Every pane's start command on the private server."""
    out = _tmux(world, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_start_command}")
    rows: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        pane_id, _, command = line.partition("\t")
        if pane_id:
            rows.append((pane_id, command))
    return rows


def _window_count(world: SmokeWorld) -> int:
    out = _tmux(world, "list-windows", "-a", "-F", "#{window_id}")
    return len([line for line in out.stdout.splitlines() if line.strip()])


def _pane_pid_alive(world: SmokeWorld, pane_id: str) -> bool:
    out = _tmux(world, "display-message", "-p", "-t", pane_id, "#{pane_pid}")
    pid = out.stdout.strip()
    if not pid.isdigit():
        return False
    probe = subprocess.run(
        ["ps", "-p", pid, "-o", "pid="], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0 and probe.stdout.strip() != ""


def _matched_turn_requests(mock) -> list:
    """The model requests that consumed the scripted turn stream."""
    return [r for r in mock.requests if r.matched and not r.structured]


def _install_isolated_codex(world: SmokeWorld) -> None:
    """Register the codex harness with this run's isolated transcript root.

    ``Daemon.__init__`` rebuilds the shipped registry, so this runs after
    every construction, in place: the observer holds this exact dict, and the
    spawner resolves the harness through it, so both see the same isolated
    harness. The manifest is the production codex manifest; only the
    transcript search root is rebound (``manifest_for_root``), keeping the
    durable reader inside the isolated CODEX_HOME instead of ~/.codex.
    """
    HARNESSES["codex"] = CodexHarness(root=world.sessions_root)


async def _aclose_daemon(daemon: Daemon) -> None:
    with contextlib.suppress(Exception):
        await daemon.aclose()


class _DaemonBusAdapter:
    """A minimal in-process régie-side client for the real PollingController.

    The controller is duck-typed on ``client.call`` and speaks only
    ``bus.tail``; this adapter answers through the production ``bus.tail`` RPC
    handler against this run's isolated daemon, so starting a second real
    socket process is unnecessary for the daemon-to-régie latency evidence.
    """

    def __init__(self, daemon: Daemon) -> None:
        self._daemon = daemon

    async def call(self, method: str, **params):
        assert method == "bus.tail", f"the régie adapter speaks only bus.tail, not {method}"
        return await usage_rpc._bus_tail(self._daemon, dict(params))


def _terminate_pid_group(pid: int) -> None:
    """Best-effort safety net for a backend the test could not tear down."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _pid_alive(pid):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)
        _await_pid_gone(pid, timeout=REAP_DEADLINE_SECONDS, what=f"pid {pid} to exit")


async def _teardown_world(world: SmokeWorld) -> None:
    """Deterministic cleanup: nothing survives the run, even on failure."""
    world.gate.set()
    for daemon in list(world.daemons):
        world.daemons.remove(daemon)
        await _aclose_daemon(daemon)
    world.mock.stop()
    for pid in list(world.backend_pids):
        world.backend_pids.remove(pid)
        with contextlib.suppress(Exception):
            _terminate_pid_group(pid)
    with contextlib.suppress(Exception):
        _tmux(world, "kill-server")
    shutil.rmtree(world.root, ignore_errors=True)


@pytest.fixture
async def world(theater_home, monkeypatch):
    """The isolated end-to-end environment; skips unless the smoke is enabled."""
    if os.environ.get(SMOKE_ENV_VAR) != "1":
        pytest.skip(
            f"opt-in release smoke: set {SMOKE_ENV_VAR}=1 to run the real codex "
            "native daemon smoke (it launches the stock codex app-server and TUI "
            "on a private tmux server); a skipped run is not release evidence"
        )
    if shutil.which("codex") is None:
        pytest.skip("codex is not on PATH")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not on PATH")

    # resolve(): the app-server canonicalises paths, and on macOS /tmp is a
    # symlink to /private/tmp — exact-cwd predicates must compare canonical
    # with canonical.
    root = Path(tempfile.mkdtemp(prefix="codex-daemon-smoke-", dir="/tmp")).resolve()
    repo = codex_env.make_git_repo(root / "repo")
    codex_home = root / "codex-home"
    tmux_root = root / "tmux-socket"
    tmux_root.mkdir()
    mock, gate = _scripted_mock()
    mock.start()
    codex_env.write_mock_config(codex_home, mock.base_url)

    # Everything the daemon and its tmux subprocesses spawn inherits this
    # environment: the private tmux socket root, the isolated CODEX_HOME for
    # the detached backend and the native UI alike, and a TERM the TUI accepts.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_root))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    built = SmokeWorld(
        home=theater_home,
        root=root,
        repo=repo,
        codex_home=codex_home,
        tmux_root=tmux_root,
        sessions_root=codex_home / "sessions",
        mock=mock,
        gate=gate,
    )
    # The isolated harness replaces the registry entry in place; the exact
    # prior entry (or its absence) is restored once every daemon has closed
    # and the run's world — including the isolated sessions root — is gone,
    # so later tests in this process never inherit a dead-root harness.
    had_prior_harness = "codex" in HARNESSES
    prior_harness = HARNESSES.get("codex")
    try:
        yield built
    finally:
        await _teardown_world(built)
        if had_prior_harness:
            HARNESSES["codex"] = prior_harness
        else:
            HARNESSES.pop("codex", None)


# ---------------------------------------------------------------------------
# The release smoke
# ---------------------------------------------------------------------------


async def test_codex_native_daemon_release_smoke(world) -> None:  # noqa: PLR0915
    """One full production chain, asserted at every seam."""
    assert codex_env.codex_version() == EXPECTED_CODEX_VERSION
    assert wiring_mod.NATIVE_AUTO_SELECTION_ENABLED is False, (
        "automatic native selection stays disabled until the Wave 5 gate"
    )

    # ---- spawn through the real RPC with explicit wiring=native ----------
    d1 = Daemon()
    world.daemons.append(d1)
    _install_isolated_codex(world)
    await d1.start()

    spawned = await spawning_rpc._spawn(
        d1,
        {
            "harness": "codex",
            "prompt": PROMPT,
            "cwd": str(world.repo),
            "approval": "yolo",
            "worktree": True,
            "wiring": "native",
        },
    )
    pid = spawned["handle"]
    assert pid == spawned["id"]

    binding = d1.store.get_runtime_binding(pid)
    assert binding is not None, "explicit wiring=native persists a runtime binding"
    assert binding.wiring is RuntimeWiring.NATIVE
    assert binding.lifecycle is RuntimeLifecyclePhase.ACTIVE, "the prompt was dispatched"
    session = binding.native_session_id
    endpoint = binding.endpoint
    backend_pid = binding.backend_pid
    started_at = binding.backend_started_at
    assert session is not None
    assert endpoint.startswith("unix://")
    assert str(world.home) in endpoint, "the private endpoint lives in the isolated home"
    assert backend_pid is not None and _pid_alive(backend_pid)
    assert started_at is not None
    world.backend_pids.append(backend_pid)

    participant = d1.registry.get(pid)
    assert participant.status is not Status.DEAD
    assert participant.tmux_pane
    assert participant.cwd != str(world.repo), "worktree=True spawned an isolated worktree"
    worktree = Path(participant.cwd)
    assert worktree.is_dir()
    # The exact UI-created session, adopted as the resume identity.
    assert participant.session_id == session
    assert participant.session_correlation == str(TranscriptProvenance.EXACT)

    # ---- stock promptless native UI attached to the same backend ----------
    ui_panes = [row for row in _ui_panes(world) if endpoint in row[1]]
    assert len(ui_panes) == 1, "exactly one native UI pane exists"
    ui_pane_id, ui_command = ui_panes[0]
    assert ui_pane_id == participant.tmux_pane
    assert ui_command.startswith("codex --remote unix://"), (
        f"unexpected promptless UI plan: {ui_command!r}"
    )
    assert "resume" not in ui_command, "a NEW spawn never carries a resume id"
    assert PROMPT not in ui_command, "the frontend plan never carries the prompt"

    # ---- the initial prompt went through ControlService exactly once ------
    running = d1.store.running_jobs_for_target(pid)
    assert [job.handle for job in running] == [pid], "the spawn job is the only job"
    assert running[0].prompt == PROMPT
    operations = d1.store.control_operations_for_job(pid)
    assert len(operations) == 1, "exactly one control operation was ever reserved"
    operation = operations[0]
    assert operation.kind is ControlKind.SEND
    assert operation.transport is ControlTransport.NATIVE_RUNTIME
    assert operation.delivery_phase is ControlDeliveryPhase.SETTLED
    assert operation.delivery_result is DeliveryResult.ACCEPTED
    assert operation.job_handle == pid, "the initial dispatch reused the spawn job"
    assert operation.native_session_id == session
    turn = operation.native_turn_id
    assert turn is not None, "the accepted native turn is durably correlated"
    operation_id = operation.operation_id

    # The scripted model request has arrived and is held on the gate: the
    # exact turn is live and mapped, and it will stay that way until release.
    await _await_until(
        lambda: len(_matched_turn_requests(world.mock)) == 1,
        timeout=MODEL_REQUEST_DEADLINE_SECONDS,
        what="the scripted model request to reach the mock",
    )
    runtime = d1.runtime_manager.get(pid)
    assert runtime is not None
    snapshot = await runtime.snapshot()
    assert snapshot.native_turn_id == turn, "the accepted turn is still active"
    assert d1.observer.live.registration_for(pid) is not None, "live wiring registered"
    assert (
        d1.store.get_native_terminal_evidence(
            participant_id=pid,
            backend_generation=binding.backend_generation,
            native_session_id=session,
            native_turn_id=turn,
        )
        is None
    ), "no terminal evidence exists while the turn is held"

    # ---- daemon-side close: disconnect only, everything survives ---------
    windows_before = _window_count(world)
    assert _pane_pid_alive(world, participant.tmux_pane)
    await d1.aclose()
    world.daemons.remove(d1)

    assert _pid_alive(backend_pid), "a healthy backend survives daemon shutdown"
    assert _pane_pid_alive(world, participant.tmux_pane), "the native UI survives it too"
    assert _window_count(world) == windows_before

    # ---- production recovery: same backend/session, no second UI ----------
    d2 = Daemon()
    world.daemons.append(d2)
    _install_isolated_codex(world)
    finish_facts: list[dict] = []
    # Latency evidence (1), endpoint: stamped only after the real finish
    # returns — by then the terminal evidence is persisted (asserted below)
    # and the exact job's done state is durable and visible to awaiters.
    daemon_done_at: list[float] = []
    real_finish = d2.jobs.finish

    def finish_spy(handle: str, **kwargs):
        if handle == pid:
            finish_facts.append(
                {
                    "state": kwargs.get("state"),
                    "evidence_persisted": d2.store.get_native_terminal_evidence(
                        participant_id=pid,
                        backend_generation=binding.backend_generation,
                        native_session_id=session,
                        native_turn_id=turn,
                    )
                    is not None,
                }
            )
            finished = real_finish(handle, **kwargs)
            if kwargs.get("state") is JobState.DONE:
                daemon_done_at.append(time.monotonic())
            return finished
        return real_finish(handle, **kwargs)

    d2.jobs.finish = finish_spy
    await d2.start()  # startup reconciliation runs before ordinary observation

    adopted = d2.store.get_runtime_binding(pid)
    assert adopted is not None
    assert adopted.native_session_id == session, "the exact native session is re-adopted"
    assert adopted.backend_pid == backend_pid, "the same verified backend is adopted"
    assert adopted.backend_started_at == started_at, "the strong start identity matched"
    assert d2.runtime_manager.get(pid) is not None, "one runtime is reconnected"
    refreshed = d2.registry.get(pid)
    assert refreshed.status is not Status.DEAD
    assert refreshed.session_id == session
    assert refreshed.tmux_pane == participant.tmux_pane
    job = d2.store.get_job(pid)
    # Rehydrated from persistence: the state is the enum's text value, so
    # compare by StrEnum equality, not identity.
    assert job is not None and job.state == JobState.RUNNING, (
        "the active job survives the restart and is never replayed"
    )
    # No second UI and no prompt replay.
    assert _window_count(world) == windows_before, "no second UI was launched"
    assert len([row for row in _ui_panes(world) if endpoint in row[1]]) == 1
    assert len(_matched_turn_requests(world.mock)) == 1, "the prompt was never replayed"
    operations_after = d2.store.control_operations_for_job(pid)
    assert [op.operation_id for op in operations_after] == [operation_id], (
        "no second control operation was ever reserved"
    )

    # ---- test-only latency seams, installed before the turn is released ----
    # (1) native event start: the exact terminal outcome's handling/queueing
    # inside the reconnected Codex runtime — never the model gate release.
    runtime2 = d2.runtime_manager.get(pid)
    assert runtime2 is not None
    native_event_at: list[float] = []
    real_record_outcome = runtime2._record_turn_outcome

    async def record_outcome_spy(sess: str, turn_id: str, terminal, **kwargs):
        if (
            not native_event_at
            and (sess, turn_id) == (session, turn)
            and terminal is NativeTurnTerminal.COMPLETED
        ):
            native_event_at.append(time.monotonic())
        return await real_record_outcome(sess, turn_id, terminal, **kwargs)

    runtime2._record_turn_outcome = record_outcome_spy

    # (2) daemon job.finished publication: the exact bus append in finish().
    publish_at: list[float] = []
    real_bus_append = d2.store.bus_append

    def bus_append_spy(kind: str, **fields):
        payload = fields.get("payload") or {}
        if not publish_at and kind == "job.finished" and payload.get("handle") == pid:
            publish_at.append(time.monotonic())
        return real_bus_append(kind, **fields)

    d2.store.bus_append = bus_append_spy

    # (2) régie observer: the real PollingController on the real default
    # RegieSection (bus_interval/bus_batch). The régie app primes the
    # animation cursor once at startup and then polls bus.tail on the
    # configured cadence — that prime and cadence are reproduced here, through
    # a minimal in-process adapter. Polling evidence only: no push path.
    regie = RegieSection()
    regie_bus_client = _DaemonBusAdapter(d2)
    regie_polling = PollingController(regie)
    primed = await regie_polling.poll_anim(regie_bus_client)
    assert primed.primed, "the régie poll primed its cursor before the turn release"
    primed_anim_cursor = regie_polling.anim_cursor

    # ---- release the turn: evidence persists before the job becomes done --
    world.gate.set()

    # The régie's polling controller observes the exact job.finished event on
    # its configured cadence; the deadline is a hang detector, not a timing
    # assertion — only bounded eventual observation is required.
    observed_row = None
    observed_at: float | None = None
    regie_deadline = time.monotonic() + REGIE_OBSERVATION_DEADLINE_SECONDS
    while observed_row is None:
        anim = await regie_polling.poll_anim(regie_bus_client)
        observed_row = next(
            (
                row
                for row in anim.rows
                if row.get("kind") == "job.finished"
                and (row.get("payload") or {}).get("handle") == pid
            ),
            None,
        )
        if observed_row is not None:
            observed_at = time.monotonic()
            break
        if time.monotonic() >= regie_deadline:
            raise AssertionError(
                f"timed out after {REGIE_OBSERVATION_DEADLINE_SECONDS}s waiting for "
                "the régie polling controller to observe the exact job.finished event"
            )
        await asyncio.sleep(regie.bus_interval)

    states = await d2.jobs.await_jobs([pid], max_wait=JOB_DEADLINE_SECONDS)
    done = states[0]
    assert done.state == JobState.DONE, f"the exact job finished: {done.state}"
    assert done.result == FINAL_MESSAGE
    assert done.error_code is None
    assert done.finished_at is not None

    evidence = d2.store.get_native_terminal_evidence(
        participant_id=pid,
        backend_generation=binding.backend_generation,
        native_session_id=session,
        native_turn_id=turn,
    )
    assert evidence is not None, "the exact native terminal evidence is persisted"
    assert evidence.terminal is NativeTurnTerminal.COMPLETED
    assert evidence.result == FINAL_MESSAGE
    assert evidence.completeness is ResultCompleteness.COMPLETE
    assert evidence.provenance is ResultProvenance.NATIVE_EVIDENCE
    assert evidence.recorded_at <= done.finished_at, "evidence was durable before done"
    assert finish_facts, "the job finished through the evidence path, once"
    assert len(finish_facts) == 1
    assert finish_facts[0]["state"] is JobState.DONE
    assert finish_facts[0]["evidence_persisted"] is True, (
        "the evidence row already existed when the job finish became visible"
    )
    assert len(_matched_turn_requests(world.mock)) == 1, "exactly one model call ever"
    assert any(world.sessions_root.rglob("rollout-*.jsonl")), (
        "the rollout lives in the isolated CODEX_HOME, not the developer's"
    )

    # ---- latency evidence: ordering, exact identity, printed labels --------
    assert len(native_event_at) == 1, "the exact terminal outcome was recorded once"
    assert len(publish_at) == 1, "the exact job.finished was published once"
    assert len(daemon_done_at) == 1, "the exact job became done exactly once"
    assert native_event_at[0] <= publish_at[0], (
        "the native event's handling precedes the job.finished publication"
    )
    assert publish_at[0] <= daemon_done_at[0], (
        "publication precedes the durable, visible job completion"
    )
    assert publish_at[0] <= observed_at, "the régie observed the event after publication"
    observed_payload = observed_row.get("payload") or {}
    assert observed_row.get("kind") == "job.finished"
    assert observed_payload.get("handle") == pid, "the exact job's event, no other"
    assert observed_payload.get("state") == "done"
    assert observed_payload.get("error_code") is None
    assert observed_row.get("id", 0) > primed_anim_cursor, (
        "the observed event postdates the primed cursor"
    )
    print(f"native_event_to_daemon_ms={(daemon_done_at[0] - native_event_at[0]) * 1000.0:.1f}")
    print(f"daemon_to_regie_ms={(observed_at - publish_at[0]) * 1000.0:.1f}")
    print(f"regie_bus_interval_ms={regie.bus_interval * 1000.0:.0f}")
    print("regie_observation=polling_cadence_only_no_push_path_claimed")

    # ---- verified teardown through the real kill --------------------------
    teardown_liveness: list[bool] = []
    real_teardown = d2.spawner.teardown

    async def teardown_spy(p, **kwargs):
        teardown_liveness.append(_pid_alive(backend_pid))
        return await real_teardown(p, **kwargs)

    d2.spawner.teardown = teardown_spy

    killed = await participants_rpc._kill(d2, {"id": pid})
    assert killed == {"id": pid, "killed": True}
    assert teardown_liveness == [False], (
        "the backend is proven stopped before the worktree is retired"
    )
    assert d2.store.get_runtime_binding(pid) is None, "the binding is swept"
    assert d2.registry.get(pid).status is Status.DEAD
    assert not worktree.is_dir(), "the worktree is retired after the proven stop"
    _await_pid_gone(backend_pid, timeout=REAP_DEADLINE_SECONDS, what="the backend pid to be reaped")
    assert not [row for row in _ui_panes(world) if endpoint in row[1]], "the native UI pane is gone"
    # Rehydrated from persistence: the state is the enum's text value.
    assert d2.store.get_job(pid).state == JobState.DONE, "kill never rewrote the job"

    await d2.aclose()
    world.daemons.remove(d2)
