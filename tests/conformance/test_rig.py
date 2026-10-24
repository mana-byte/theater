"""The rig blames the stock CLI only when every suite component is proven alive."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.harness_compat.mock import MockServer
from tests.support import live_rig, live_stack
from tests.support.live_rig import LiveRig, LiveSession
from tests.support.live_stack import Stack


class _Jobs:
    """Reports whatever job state a test sets, as the SDK's await would."""

    def __init__(self) -> None:
        self.job = SimpleNamespace(state="running", error=None)

    async def await_(self, handles: list[str], *, wait_seconds: float) -> Any:
        return SimpleNamespace(value=SimpleNamespace(jobs=[self.job]))


class _Client:
    """Just what the rig reads: a connection flag, the provider registry and job states."""

    def __init__(self, *healths: str, connected: bool = True, unreadable: bool = False) -> None:
        self.connected = connected
        self._healths = healths
        self._unreadable = unreadable
        self.providers = self
        self.jobs = _Jobs()

    async def list(self) -> Any:
        if self._unreadable:
            raise OSError("registry unreadable")
        items = tuple(SimpleNamespace(health=health) for health in self._healths)
        return SimpleNamespace(value=SimpleNamespace(items=items))


def _rig(
    tmp_path: Path, client: _Client, *, daemon: bool = True, mock_serving: bool = True
) -> tuple[LiveRig, MockServer]:
    session = LiveSession("acme", Path("acme"), "1", tmp_path, tmp_path / "e", "token")
    mock = MockServer(tmp_path / "requests.jsonl")
    if mock_serving:
        mock.start()
    run = tmp_path / "th" / "var" / "run"
    run.mkdir(parents=True)
    if daemon:
        (run / "daemon.pid").write_text(str(os.getpid()))
    rig = LiveRig(
        session,
        cast(Any, None),
        Stack(tmp_path, {"TMUX_TMPDIR": str(tmp_path / "t")}),
        cast(Any, None),
        mock,
        cast(Any, client),
        tmp_path / "w",
        tmp_path / "m",
        "scenario",
    )
    return rig, mock


@pytest.mark.parametrize(
    ("setup", "tmux", "broken"),
    [
        pytest.param({"daemon": False}, True, "the daemon", id="daemon-gone"),
        pytest.param(
            {"client": _Client("online", connected=False)},
            True,
            "daemon connection",
            id="disconnected",
        ),
        pytest.param({"mock_serving": False}, True, "the mock model server", id="mock-stopped"),
        pytest.param({}, False, "the private tmux server", id="tmux-gone"),
        pytest.param({"client": _Client()}, True, "the terminal provider", id="no-provider"),
        pytest.param({"client": _Client("offline")}, True, "the terminal provider", id="offline"),
        pytest.param(
            {"client": _Client(unreadable=True)}, True, "provider registry", id="unreadable"
        ),
        pytest.param({}, True, None, id="all-alive"),
    ],
)
async def test_a_component_not_proven_alive_is_named_and_never_blamed_on_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: dict[str, Any],
    tmux: bool,
    broken: str | None,
) -> None:
    monkeypatch.setattr(Stack, "tmux_running", lambda _self: tmux)
    rig, mock = _rig(
        tmp_path,
        setup.get("client", _Client("online")),
        daemon=setup.get("daemon", True),
        mock_serving=setup.get("mock_serving", True),
    )
    try:
        found = await rig.broken_component()
    finally:
        mock.stop()

    assert (found is None) if broken is None else (found is not None and broken in found)


async def test_an_exit_with_a_broken_component_is_inconclusive_not_a_cli_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Stack, "tmux_running", lambda _self: True)
    rig, mock = _rig(tmp_path, _Client("online"), mock_serving=False)

    with pytest.raises(pytest.skip.Exception, match="the mock model server stopped"):
        await rig._classify_exit("p1")
    mock.start()
    try:
        with pytest.raises(pytest.fail.Exception, match="the stock CLI exited before readiness"):
            await rig._classify_exit("p1")
    finally:
        mock.stop()


def _write_logs(theater_home: Path) -> None:
    logs = theater_home / "regie" / "var" / "logs"
    logs.mkdir(parents=True)
    (logs / "bridge.log").write_text("bridge said this", encoding="utf-8")


def test_a_stack_that_never_started_leaves_no_evidence_directory(tmp_path: Path) -> None:
    Stack(tmp_path, {})._copy_logs(tmp_path / "evidence" / "boot")

    assert not (tmp_path / "evidence").exists()


def test_capturing_evidence_copies_logs_and_every_pane_and_survives_a_missing_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture is best effort: it runs at teardown, however far the stack got."""
    stack, target = Stack(tmp_path, {}), tmp_path / "evidence"
    _write_logs(tmp_path / "th")
    monkeypatch.setattr(Stack, "panes", lambda _self: ["%1", "%2"])
    monkeypatch.setattr(Stack, "tmux", lambda _self, *args: f"screen of {args[3]}")

    stack.capture(target)

    assert (target / "bridge.log").read_text(encoding="utf-8") == "bridge said this"
    assert (target / "pane-2.txt").read_text(encoding="utf-8") == "screen of %2"

    def missing(_self: Stack) -> list[str]:
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(Stack, "panes", missing)
    stack.capture(tmp_path / "other")  # must not raise: a stack that never started has no tmux


def _scripted_boot(monkeypatch: pytest.MonkeyPatch, *outcomes: str | None) -> list[str]:
    """Boot attempts that end as scripted; what the stack was asked to do between them."""
    attempts = iter(outcomes)
    steps: list[str] = []
    monkeypatch.setattr(Stack, "_start", lambda _self: next(attempts))
    monkeypatch.setattr(Stack, "stop", lambda _self: steps.append("stop"))
    return steps


def test_a_boot_that_is_retried_keeps_the_logs_of_the_attempt_it_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    _write_logs(tmp_path / "th")
    steps = _scripted_boot(monkeypatch, "the Regie bridge did not start: slow", None)

    assert Stack(tmp_path, {}).boot(evidence) is None

    assert (evidence / "boot-1" / "bridge.log").read_text(encoding="utf-8") == "bridge said this"
    assert steps == ["stop"] and [path.name for path in evidence.iterdir()] == ["boot-1"]


def test_two_missed_boots_report_the_last_reason_and_keep_the_last_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    _write_logs(tmp_path / "th")
    steps = _scripted_boot(monkeypatch, "first miss", "second miss")

    assert Stack(tmp_path, {}).boot(evidence) == "second miss"

    assert sorted(path.name for path in evidence.iterdir()) == ["boot", "boot-1"]
    assert steps == ["stop"], "the last attempt is left for the caller's cleanup, logs kept"


async def test_a_presence_refusal_on_an_unattended_rig_is_inconclusive_not_a_cli_failure(
    tmp_path: Path,
) -> None:
    client = _Client("online")
    rig, mock = _rig(tmp_path, client)
    client.jobs.job = SimpleNamespace(state="crashed", error=SimpleNamespace(code="human_present"))
    try:
        with pytest.raises(pytest.skip.Exception, match="human_present on an unattended rig"):
            await rig.job("j1")
        client.jobs.job = SimpleNamespace(state="crashed", error=SimpleNamespace(code="busy"))
        assert (await rig.job("j1")).error.code == "busy"
    finally:
        mock.stop()


async def test_a_send_waits_for_presence_to_read_absent_and_stay_settled(tmp_path: Path) -> None:
    rig, mock = _rig(tmp_path, _Client("online"))
    readings = iter(["unknown", "present"])
    reported: list[str] = []

    async def participant(_participant_id: str) -> Any:
        reported.append(next(readings, "absent"))
        return SimpleNamespace(status="idle", presence=reported[-1])

    async def controls(_participant_id: str) -> Any:
        return SimpleNamespace(
            actions={"send": SimpleNamespace(admissible=True, reason="", detail="")}
        )

    rig.participant = participant  # type: ignore[method-assign]
    rig.controls = controls  # type: ignore[method-assign]
    try:
        await rig.ready("p1", seconds=15)
    finally:
        mock.stop()

    assert reported[:2] == ["unknown", "present"]
    assert reported[2:] and set(reported[2:]) == {"absent"}, "readiness must hold, not flash"
    assert len(reported) >= 2 + 1.0 / 0.25, "readiness settles for a full window of polls"


def _reaped(*script: str) -> tuple[subprocess.Popen[str], int]:
    """A process that has printed its first line, reaped on exit as the rig's daemon is."""
    process = subprocess.Popen(
        [sys.executable, "-c", "\n".join(script)], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout is not None and process.stdout.readline().strip() == "ready"
    threading.Thread(target=process.wait, daemon=True).start()
    return process, process.pid


@pytest.mark.parametrize(
    ("handler", "graceful"), [("signal.SIG_DFL", True), ("signal.SIG_IGN", False)]
)
def test_a_daemon_that_ignores_sigterm_is_killed_and_reported_as_such(
    monkeypatch: pytest.MonkeyPatch, handler: str, graceful: bool
) -> None:
    monkeypatch.setattr(live_stack, "STOP_SECONDS", 1.0)
    process, pid = _reaped(
        "import signal, time",
        f"signal.signal(signal.SIGTERM, {handler})",
        "print('ready', flush=True)",
        "time.sleep(60)",
    )

    assert live_stack._terminate(pid) is graceful

    deadline = time.monotonic() + 5
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process.poll() is not None


def test_a_restarted_daemon_is_detached_with_the_rig_environment_and_the_same_start_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: dict[str, Any] = {}

    class Popen:
        def __init__(self, argv: list[str], **options: Any) -> None:
            started.update(argv=argv, **options)
            options["stdout"].write(b"second daemon\n")

        def wait(self) -> int:
            return 0

    log = tmp_path / "th" / live_stack.DAEMON_START_LOG
    log.parent.mkdir(parents=True)
    log.write_text("first daemon\n", encoding="utf-8")
    monkeypatch.setattr(live_stack.subprocess, "Popen", Popen)
    env = {"THEATER_HOME": str(tmp_path / "th"), "HOME": str(tmp_path / "h")}

    Stack(tmp_path, env).start_daemon()

    assert started["argv"] == [sys.executable, "-m", "theater.cli", "daemon"]
    assert started["start_new_session"] is True and started["env"] == env
    assert started["cwd"] == live_stack.REPO_ROOT
    assert log.read_text(encoding="utf-8") == "first daemon\nsecond daemon\n"


class _Daemon(_Client):
    """A client of a daemon still starting: refuses connections, lists its provider offline."""

    def __init__(self, steps: list[str], *, refusals: int = 0, offline_polls: int = 0) -> None:
        super().__init__("online")
        self.steps, self.refusals, self.offline_polls = steps, refusals, offline_polls

    async def connect(self) -> None:
        if self.refusals:
            self.refusals -= 1
            raise ConnectionRefusedError

    async def close(self) -> None:
        self.steps.append("close")

    async def list(self) -> Any:
        self._healths = ("offline",) if self.offline_polls > 0 else ("online",)
        self.offline_polls -= 1
        return await super().list()


async def test_restarting_the_daemon_replaces_the_client_once_daemon_and_provider_are_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps: list[str] = []
    old, refusing = _Daemon(steps), _Daemon(steps, refusals=1)
    answering = _Daemon(steps, offline_polls=2)
    clients = [refusing, answering]
    monkeypatch.setattr(Stack, "stop_daemon", lambda _self: steps.append("stop") or True)
    monkeypatch.setattr(Stack, "start_daemon", lambda _self: steps.append("start"))
    monkeypatch.setattr(live_rig, "_client", lambda socket: clients.pop(0))
    rig, mock = _rig(tmp_path, cast(Any, old))
    try:
        await rig.restart_daemon()
    finally:
        mock.stop()

    # The old client closes before the daemon stops; the refused attempt closes its own.
    assert steps == ["close", "stop", "start", "close"]
    assert rig.client is answering and clients == []
    assert answering.offline_polls < 0, "the restart waited for the provider to come back"


@pytest.mark.parametrize(
    ("terminated", "late", "reason"),
    [
        pytest.param(False, {}, "did not exit on SIGTERM", id="stuck"),
        pytest.param(True, {"refusals": 10**6}, "did not answer", id="no-daemon"),
        pytest.param(True, {"offline_polls": 10**6}, "provider did not reconnect", id="no-bridge"),
    ],
)
async def test_a_stack_that_cannot_be_restarted_is_inconclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminated: bool,
    late: dict[str, int],
    reason: str,
) -> None:
    steps: list[str] = []
    monkeypatch.setattr(Stack, "stop_daemon", lambda _self: terminated)
    monkeypatch.setattr(Stack, "start_daemon", lambda _self: steps.append("start"))
    monkeypatch.setattr(live_rig, "_client", lambda socket: _Daemon(steps, **late))
    monkeypatch.setattr(live_rig, "BOOT_SECONDS", 0.3)
    rig, mock = _rig(tmp_path, cast(Any, _Daemon(steps)))
    try:
        with pytest.raises(pytest.skip.Exception, match=f"inconclusive: .*{reason}"):
            await rig.restart_daemon()
    finally:
        mock.stop()

    assert ("start" in steps) is terminated


class _Parts:
    """Fakes for what ``LiveRig.open`` composes, all recording into one ordered list."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, boot: str | None, connect: bool) -> None:
        self.steps: list[str] = []
        steps = self.steps

        class FakeMock:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                self.base_urls: dict[str, str] = {}

            def start(self) -> None:
                steps.append("mock start")

            def stop(self) -> None:
                steps.append("mock stop")

        class FakeStack:
            def __init__(self, root: Path, env: dict[str, str]) -> None:
                self.socket = root / "daemon.sock"

            def boot(self, evidence: Path) -> str | None:
                steps.append("boot")
                return boot

            def capture(self, target: Path) -> None:
                steps.append("capture")

            def stop(self) -> None:
                steps.append("stop")

        class FakeClient:
            async def connect(self) -> None:
                steps.append("connect")
                if not connect:
                    raise ConnectionRefusedError

            async def close(self) -> None:
                steps.append("close")

        monkeypatch.setattr(live_rig, "MockServer", FakeMock)
        monkeypatch.setattr(live_rig, "Stack", FakeStack)
        monkeypatch.setattr(live_rig, "_prepare", lambda *args: (None, {}))
        monkeypatch.setattr(live_rig, "_client", lambda socket: FakeClient())


def _open(tmp_path: Path):
    session = LiveSession("acme", Path("acme"), "1", tmp_path, tmp_path / "e", "token")
    return LiveRig.open(session, cast(Any, SimpleNamespace(compat=object())), label="scenario")


@pytest.mark.parametrize(
    ("boot", "connect", "outcome", "expected"),
    [
        pytest.param(
            "the Regie bridge did not start",
            True,
            pytest.skip.Exception,
            ["mock start", "boot", "capture", "stop", "mock stop"],
            id="boot-fails",
        ),
        pytest.param(
            None,
            False,
            ConnectionRefusedError,
            ["mock start", "boot", "connect", "close", "capture", "stop", "mock stop"],
            id="connect-fails",
        ),
        pytest.param(
            None,
            True,
            None,
            ["mock start", "boot", "connect", "close", "capture", "stop", "mock stop"],
            id="scenario-runs",
        ),
    ],
)
async def test_the_rig_undoes_what_came_up_in_reverse_however_far_it_got(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boot: str | None,
    connect: bool,
    outcome: type[BaseException] | None,
    expected: list[str],
) -> None:
    parts = _Parts(monkeypatch, boot=boot, connect=connect)

    if outcome is None:
        async with _open(tmp_path):
            pass
    else:
        with pytest.raises(outcome):
            async with _open(tmp_path):
                pytest.fail("the scenario must not start")

    assert parts.steps == expected
