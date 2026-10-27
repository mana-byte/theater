"""The Régie CLI lifecycle: daemon start, bridge start/stop/status, and the tmux entry paths."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest
from regie import process
from regie.cli import _parser
from regie.contracts import BridgeStatus
from regie.paths import RegiePaths, paths_from_environment
from regie.process import (
    BridgeProcessManager,
    BridgeProcessStatus,
    connect_or_start_daemon,
)

_SERVER_IDENTITY = '["/tmp/tmux/default","10","20"]'


@pytest.fixture(autouse=True)
def tmux_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        globals(), "_SERVER_IDENTITY", json.dumps([str(tmp_path / "tmux.sock"), "10", "20"])
    )


class _Client:
    def __init__(self, _socket: Path, **_kwargs: object) -> None:
        self.closed = False

    async def connect(self) -> object:
        return object()

    async def close(self) -> None:
        self.closed = True


def test_regie_paths_are_private_children_of_the_selected_theater_home(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path))

    paths = paths_from_environment()

    assert paths.config_path == tmp_path / "regie" / "config.toml"
    assert paths.daemon_socket == tmp_path / "var" / "run" / "daemon.sock"
    assert paths.bridge_status_path == tmp_path / "regie" / "var" / "run" / "bridge.status.json"
    assert paths.ui_log_path.parent == tmp_path / "regie" / "var" / "logs" / "ui"  # not Theater's


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_absent_daemon_starts_the_installed_theater_once(tmp_path: Path) -> None:
    started = False
    launches: list[tuple[list[str], dict[str, object]]] = []

    class AbsentThenReady(_Client):
        async def connect(self) -> object:
            if not started:
                raise FileNotFoundError("missing socket")
            return object()

    def popen(command: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal started
        started = True
        launches.append((command, kwargs))
        return SimpleNamespace(pid=101, poll=lambda: None, terminate=lambda: None)

    client, _handshake = await connect_or_start_daemon(
        socket_path=tmp_path / "daemon.sock",
        client_id="regie-test",
        required_capabilities=("orchestration.v1",),
        log_path=tmp_path / "daemon.log",
        client_factory=AbsentThenReady,
        popen_factory=popen,
        sleep=_no_sleep,
    )

    assert launches[0][0][1:] == ["-m", "theater.cli", "daemon"]
    assert launches[0][1]["start_new_session"] is True
    await client.close()


async def test_compatible_daemon_is_used_without_replacement(tmp_path: Path) -> None:
    def unexpected_start(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise AssertionError("a reachable compatible daemon must not be replaced")

    client, _handshake = await connect_or_start_daemon(
        socket_path=tmp_path / "daemon.sock",
        client_id="regie-test",
        required_capabilities=(),
        log_path=tmp_path / "daemon.log",
        client_factory=_Client,
        popen_factory=unexpected_start,
    )

    await client.close()


def _online(
    paths: RegiePaths, *, pid: int = 123, token: str = "bridge-token", build: str = process._BUILD
) -> None:
    process._write_pid(paths.bridge_pid_path, pid, token)
    process._write_status(
        paths.bridge_status_path,
        BridgeProcessStatus(
            running=True,
            connection_state="online",
            pid=pid,
            provider_id="provider-tmux",
            provider_generation=4,
            tmux_server_identity=_SERVER_IDENTITY,
            token=token,
            build=build,
        ),
    )


def test_bridge_start_is_idempotent_after_readiness(tmp_path: Path, monkeypatch) -> None:
    paths = RegiePaths(tmp_path)
    launches: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("TMUX", "/tmp/tmux/default,10,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(process, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process, "_lock_held", lambda _path: True)
    proofs: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        process, "_bridge_worker_matches", lambda *args: proofs.append(args) or True
    )

    def popen(command: list[str], **kwargs: object) -> SimpleNamespace:
        token = command[command.index("--token") + 1]
        _online(paths, token=token)
        launches.append((command, kwargs))
        return SimpleNamespace(pid=123, poll=lambda: None, terminate=lambda: None)

    manager = BridgeProcessManager(
        paths,
        socket_path=tmp_path / "daemon.sock",
        popen_factory=popen,
        sleep=lambda _seconds: None,
    )

    assert manager.start(timeout=5.0).connection_state == "online"
    assert manager.start(timeout=5.0).connection_state == "online"
    assert len(launches) == 1
    assert len(proofs) == 2
    command, kwargs = launches[0]
    assert command[1:3] == ["-m", "regie"]
    assert kwargs["env"]["TMUX"] == "/tmp/tmux/default,10,0"
    assert "TMUX_PANE" not in kwargs["env"]


def test_bridge_worker_command_parses_when_the_urlsafe_token_starts_with_a_dash(
    tmp_path: Path, monkeypatch
) -> None:
    """argparse reads a leading "-" as an option: 1 token in 64 killed the worker unseen."""
    paths = RegiePaths(tmp_path)
    monkeypatch.setattr(process.secrets, "token_urlsafe", lambda _n: "-" + "A" * 31)
    monkeypatch.setattr(process, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process, "_lock_held", lambda _path: True)
    monkeypatch.setattr(process, "_bridge_worker_matches", lambda *_args: True)
    commands: list[list[str]] = []

    def popen(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        _online(paths, token=command[command.index("--token") + 1])
        return SimpleNamespace(pid=123, poll=lambda: None, terminate=lambda: None)

    BridgeProcessManager(
        paths, socket_path=tmp_path / "daemon.sock", popen_factory=popen, sleep=lambda _s: None
    ).start(timeout=5.0)

    worker = _parser().parse_args(commands[0][3:])  # past "python -m regie"
    assert worker.token == commands[0][commands[0].index("--token") + 1]


def test_bridge_start_reports_a_worker_that_exited_instead_of_a_timeout(
    tmp_path: Path,
) -> None:
    paths = RegiePaths(tmp_path)
    manager = BridgeProcessManager(
        paths,
        socket_path=tmp_path / "daemon.sock",
        popen_factory=lambda *_a, **_k: SimpleNamespace(
            pid=123, poll=lambda: 2, terminate=lambda: None
        ),
        sleep=lambda _s: None,
    )

    with pytest.raises(process.BridgeStartupError, match="exited with status 2"):
        manager.start(timeout=5.0)


async def test_bridge_worker_preserves_status_change_published_during_write(
    tmp_path: Path, monkeypatch
) -> None:
    paths = RegiePaths(tmp_path)
    writes: list[BridgeProcessStatus] = []
    bridge = None

    class Bridge:
        def __init__(self, _config: object) -> None:
            nonlocal bridge
            bridge = self
            self.status_changed = asyncio.Event()
            self.finished = asyncio.Event()
            self.status = BridgeStatus(running=True, connection_state="starting")

        async def run(self) -> None:
            await self.finished.wait()

        async def close(self) -> None:
            self.finished.set()

        def publish_online(self) -> None:
            self.status = BridgeStatus(
                running=True,
                connection_state="online",
                provider_id="provider-tmux",
                provider_generation=3,
                process_id=123,
                tmux_server_identity=_SERVER_IDENTITY,
            )
            self.status_changed.set()

    def write_status(_path: Path, status: BridgeProcessStatus) -> None:
        writes.append(status)
        if len(writes) == 2:
            assert bridge is not None
            bridge.publish_online()
        elif status.connection_state == "online":
            assert bridge is not None
            bridge.finished.set()

    monkeypatch.setattr("regie.bridge.runtime.TmuxBridge", Bridge)
    monkeypatch.setattr(process, "_write_status", write_status)

    result = await asyncio.wait_for(
        process.run_bridge_worker(
            paths=paths,
            socket_path=tmp_path / "daemon.sock",
            selector="tmux",
            client_id="regie-test",
            token="bridge-token",
        ),
        timeout=5.0,
    )

    assert result == 0
    assert [status.connection_state for status in writes] == [
        "starting",
        "starting",
        "online",
        "stopped",
    ]


def test_bridge_start_replaces_a_bridge_from_another_installation(
    tmp_path: Path, monkeypatch
) -> None:
    paths = RegiePaths(tmp_path)
    paths.ensure_private_runtime()
    _online(paths, build="/nix/store/previous-env/bin/python3.12")
    monkeypatch.setattr(process, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process, "_lock_held", lambda _path: True)
    monkeypatch.setattr(process, "_bridge_worker_matches", lambda *_args: True)
    launches: list[str] = []

    def popen(command: list[str], **_kwargs: object) -> SimpleNamespace:
        token = command[command.index("--token") + 1]
        _online(paths, token=token)
        launches.append(token)
        return SimpleNamespace(pid=123, poll=lambda: None, terminate=lambda: None)

    manager = BridgeProcessManager(
        paths, socket_path=tmp_path / "daemon.sock", popen_factory=popen, sleep=lambda _s: None
    )
    stopped: list[bool] = []
    monkeypatch.setattr(
        manager, "stop", lambda **_kwargs: stopped.append(True) or BridgeProcessStatus.stopped()
    )

    assert manager.start(timeout=5.0).build == process._BUILD
    assert stopped == [True] and len(launches) == 1


def test_bridge_stop_signals_only_a_verified_bridge_process(tmp_path: Path, monkeypatch) -> None:
    paths = RegiePaths(tmp_path)
    paths.ensure_private_runtime()
    _online(paths)
    alive = iter((True, True, False, False))
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process, "_pid_alive", lambda _pid: next(alive))
    monkeypatch.setattr(process, "_lock_held", lambda _path: True)
    monkeypatch.setattr(process, "_bridge_worker_matches", lambda *_args: True)
    monkeypatch.setattr(process.os, "kill", lambda pid, value: signals.append((pid, value)))

    result = BridgeProcessManager(
        paths, socket_path=tmp_path / "daemon.sock", sleep=lambda _seconds: None
    ).stop(timeout=5.0)

    assert result.running is False
    assert signals == [(123, signal.SIGTERM)]
    assert not paths.bridge_pid_path.exists()


async def test_worker_releases_its_lock_when_socket_validation_fails(tmp_path, monkeypatch) -> None:
    paths = RegiePaths(tmp_path)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "missing"))
    assert (
        await process.run_bridge_worker(
            paths=paths,
            socket_path=paths.daemon_socket,
            selector="tmux",
            client_id="regie-test",
            token="test-token",
        )
        == 1
    )
    assert not paths.bridge_pid_path.exists()
    lease = process._FileLock(paths.bridge_process_lock)
    lease.acquire()
    lease.release()
