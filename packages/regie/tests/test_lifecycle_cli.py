from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest
from regie import cli, process
from regie.paths import RegiePaths, paths_from_environment
from regie.process import (
    BridgeProcessManager,
    BridgeProcessStatus,
    IncompatibleDaemon,
    RegieStartupError,
    connect_or_start_daemon,
)

from theater.frontend import NegotiationError

_SERVER_IDENTITY = '["/tmp/tmux/default","10","20"]'


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
    assert paths.bridge_status_path == tmp_path / "regie" / "bridge.status.json"


def test_ui_log_pruning_precedes_logger_configuration_on_the_bridge_server(
    tmp_path: Path, monkeypatch
) -> None:
    paths = RegiePaths(tmp_path)
    paths.ensure_private_runtime()
    calls: list[tuple[str, object]] = []

    async def live_panes(server_identity: str) -> tuple[str, ...]:
        calls.append(("panes", server_identity))
        return ("%7", "%9")

    class Handle:
        def close(self) -> None:
            return None

    def prune(directory: Path, current: Path, *, protected: tuple[str, ...]) -> int:
        calls.append(("prune", (directory, current, protected)))
        return 0

    def configure(path: Path) -> Handle:
        calls.append(("configure", path))
        return Handle()

    monkeypatch.setattr(cli.tmux_bootstrap, "live_pane_ids", live_panes)
    monkeypatch.setattr(cli, "prune_regie_generations", prune)
    monkeypatch.setattr(cli, "configure_logging", configure)

    cli._configure_ui_logging(paths, _SERVER_IDENTITY).close()

    assert calls == [
        ("panes", _SERVER_IDENTITY),
        ("prune", (paths.logs_dir, paths.ui_log_path, ("%7", "%9"))),
        ("configure", paths.ui_log_path),
    ]


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


async def test_incompatible_daemon_is_reported_without_replacement(tmp_path: Path) -> None:
    class Incompatible(_Client):
        async def connect(self) -> object:
            raise NegotiationError("missing state.follow.v1")

    def unexpected_start(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise AssertionError("an incompatible daemon must not be replaced")

    with pytest.raises(IncompatibleDaemon, match="will not replace"):
        await connect_or_start_daemon(
            socket_path=tmp_path / "daemon.sock",
            client_id="regie-test",
            required_capabilities=("state.follow.v1",),
            log_path=tmp_path / "daemon.log",
            client_factory=Incompatible,
            popen_factory=unexpected_start,
        )


async def test_silent_daemon_is_bounded_and_never_replaced(tmp_path: Path) -> None:
    class Silent(_Client):
        async def connect(self) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    def unexpected_start(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise AssertionError("a reachable silent daemon must not be replaced")

    with pytest.raises(RegieStartupError, match="did not answer within"):
        await connect_or_start_daemon(
            socket_path=tmp_path / "daemon.sock",
            client_id="regie-test",
            required_capabilities=(),
            log_path=tmp_path / "daemon.log",
            timeout=0.01,
            client_factory=Silent,
            popen_factory=unexpected_start,
        )


def _online(paths: RegiePaths, *, pid: int = 123, token: str = "bridge-token") -> None:
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
        ),
    )


def test_bridge_start_is_idempotent_after_readiness(tmp_path: Path, monkeypatch) -> None:
    paths = RegiePaths(tmp_path)
    launches: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("TMUX", "/tmp/tmux/default,10,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(process, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process, "_lock_held", lambda _path: True)
    monkeypatch.setattr(process, "_bridge_worker_matches", lambda *_args: True)

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

    assert manager.start(timeout=0.1).connection_state == "online"
    assert manager.start(timeout=0.1).connection_state == "online"
    assert len(launches) == 1
    command, kwargs = launches[0]
    assert command[1:3] == ["-m", "regie"]
    assert kwargs["env"]["TMUX"] == "/tmp/tmux/default,10,0"
    assert "TMUX_PANE" not in kwargs["env"]


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
    ).stop(timeout=0.1)

    assert result.running is False
    assert signals == [(123, signal.SIGTERM)]
    assert not paths.bridge_pid_path.exists()


def test_bridge_stop_refuses_an_unverified_pid(tmp_path: Path, monkeypatch) -> None:
    paths = RegiePaths(tmp_path)
    paths.ensure_private_runtime()
    _online(paths)
    monkeypatch.setattr(process, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process, "_lock_held", lambda _path: True)
    monkeypatch.setattr(process, "_bridge_worker_matches", lambda *_args: False)
    monkeypatch.setattr(
        process.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unverified PID was signalled")),
    )

    status = BridgeProcessManager(paths, socket_path=tmp_path / "daemon.sock").stop()

    assert status.running is False
    assert status.detail == "the recorded bridge process is no longer verified"


def test_bridge_status_and_stop_never_probe_or_start_theater(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    paths = RegiePaths(tmp_path)

    def unexpected_probe(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bridge status and stop must not touch Theater")

    monkeypatch.setattr(cli, "paths_from_environment", lambda: paths)
    monkeypatch.setattr(cli, "connect_or_start_daemon", unexpected_probe)

    assert cli.main(["bridge", "status"]) == 0
    assert cli.main(["bridge", "stop"]) == 0
    assert capsys.readouterr().out.count('"running": false') == 2
    assert not paths.root.exists()


def test_regie_inside_tmux_starts_services_then_tui_and_detaches(
    tmp_path: Path, monkeypatch
) -> None:
    paths = RegiePaths(tmp_path)
    calls: list[tuple[str, object]] = []
    settings = object()

    class Manager:
        def __init__(self, _paths: RegiePaths, *, socket_path: Path) -> None:
            assert socket_path == paths.daemon_socket

        def start(self) -> BridgeProcessStatus:
            calls.append(("bridge", None))
            return BridgeProcessStatus(
                running=True,
                connection_state="online",
                tmux_server_identity=_SERVER_IDENTITY,
            )

    def load(path: Path) -> object:
        calls.append(("config", path))
        return settings

    def probe(_paths: RegiePaths, socket_path: Path, client_id: str) -> None:
        calls.append(("probe", (socket_path, client_id)))

    async def require_current_pane(server_identity: str) -> str:
        calls.append(("pane", server_identity))
        return "@1"

    async def sync_color_environment(server_identity: str) -> None:
        calls.append(("colors", server_identity))

    def app(
        socket_path: Path,
        client_id: str,
        settings: object,
        server_identity: str,
    ) -> None:
        calls.append(("app", (socket_path, client_id, settings, server_identity)))

    monkeypatch.setattr(cli, "paths_from_environment", lambda: paths)
    monkeypatch.setattr(cli, "BridgeProcessManager", Manager)
    monkeypatch.setattr(cli, "load_settings", load)
    monkeypatch.setattr(cli, "_probe_daemon", probe)
    monkeypatch.setattr(cli, "_run_app", app)
    monkeypatch.setattr(cli.tmux_bootstrap, "available", lambda: True)
    monkeypatch.setattr(cli.tmux_bootstrap, "current_pane_id", lambda: "%7")
    monkeypatch.setattr(cli.tmux_bootstrap, "require_current_pane", require_current_pane)
    monkeypatch.setattr(
        cli.tmux_bootstrap,
        "sync_color_environment",
        sync_color_environment,
    )
    monkeypatch.setattr(
        cli.tmux_bootstrap,
        "detach_current_client",
        lambda: calls.append(("detach", None)),
    )

    assert cli.main(["--client-id", "operator-ui"]) == 0
    assert [name for name, _value in calls] == [
        "config",
        "probe",
        "bridge",
        "pane",
        "colors",
        "app",
        "detach",
    ]
    assert calls[0][1] == paths.config_path
    assert calls[1][1] == (paths.daemon_socket, "operator-ui")
    assert calls[3][1] == _SERVER_IDENTITY
    assert calls[4][1] == _SERVER_IDENTITY
    assert calls[5][1] == (
        paths.daemon_socket,
        "operator-ui",
        settings,
        _SERVER_IDENTITY,
    )


def test_regie_outside_tmux_starts_services_then_attaches_exact_bridge_server(
    tmp_path: Path, monkeypatch
) -> None:
    paths = RegiePaths(tmp_path)
    calls: list[tuple[str, object]] = []
    settings = object()

    class Manager:
        def __init__(self, _paths: RegiePaths, *, socket_path: Path) -> None:
            assert socket_path == paths.daemon_socket

        def start(self) -> BridgeProcessStatus:
            calls.append(("bridge", None))
            return BridgeProcessStatus(
                running=True,
                connection_state="online",
                tmux_server_identity=_SERVER_IDENTITY,
            )

    monkeypatch.setattr(cli, "paths_from_environment", lambda: paths)
    monkeypatch.setattr(cli, "BridgeProcessManager", Manager)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda path: calls.append(("config", path)) or settings,
    )
    monkeypatch.setattr(
        cli,
        "_probe_daemon",
        lambda _paths, socket_path, client_id: calls.append(("probe", (socket_path, client_id))),
    )
    monkeypatch.setattr(cli.tmux_bootstrap, "available", lambda: True)
    monkeypatch.setattr(cli.tmux_bootstrap, "current_pane_id", lambda: None)
    monkeypatch.setattr(
        cli,
        "_configure_ui_logging",
        lambda *_args: (_ for _ in ()).throw(AssertionError("outer launcher pruned UI logs")),
    )
    monkeypatch.setattr(
        cli.tmux_bootstrap,
        "launch_regie_session",
        lambda cwd, **kwargs: calls.append(("launch", (cwd, kwargs))),
    )
    monkeypatch.setattr(
        cli,
        "_run_app",
        lambda *_args: (_ for _ in ()).throw(AssertionError("TUI ran outside tmux")),
    )

    assert cli.main(["--client-id", "operator-ui"]) == 0
    assert [name for name, _value in calls] == ["config", "probe", "bridge", "launch"]
    _cwd, launch = calls[-1][1]
    assert launch["expected_server_identity"] == _SERVER_IDENTITY
    assert launch["command"] == (
        cli.sys.executable,
        "-m",
        "regie",
        "--socket",
        str(paths.daemon_socket),
        "--client-id",
        "operator-ui",
    )


def test_regie_refuses_missing_tmux_before_starting_daemon(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    paths = RegiePaths(tmp_path)
    monkeypatch.setattr(cli, "paths_from_environment", lambda: paths)
    monkeypatch.setattr(cli.tmux_bootstrap, "available", lambda: False)
    monkeypatch.setattr(
        cli,
        "_probe_daemon",
        lambda *_args: (_ for _ in ()).throw(AssertionError("daemon was started")),
    )

    assert cli.main([]) == 1
    assert "tmux is not on PATH" in capsys.readouterr().err


def test_regie_refuses_bridge_without_server_identity(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = RegiePaths(tmp_path)

    class Manager:
        def __init__(self, _paths: RegiePaths, *, socket_path: Path) -> None:
            pass

        def start(self) -> BridgeProcessStatus:
            return BridgeProcessStatus(running=True, connection_state="online")

    monkeypatch.setattr(cli, "paths_from_environment", lambda: paths)
    monkeypatch.setattr(cli, "BridgeProcessManager", Manager)
    monkeypatch.setattr(cli, "load_settings", lambda _path: object())
    monkeypatch.setattr(cli, "_probe_daemon", lambda *_args: None)
    monkeypatch.setattr(cli.tmux_bootstrap, "available", lambda: True)

    assert cli.main([]) == 1
    assert "without a pinned tmux server identity" in capsys.readouterr().err
