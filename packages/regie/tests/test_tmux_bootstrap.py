from __future__ import annotations

import pytest
from regie.tmux import bootstrap
from regie.tmux.command import TmuxError
from regie.tmux.identity import PaneSnapshot, ServerIdentity

_SOCKET = "/tmp/tmux-test/default"
_SERVER = ServerIdentity(_SOCKET, "123", "456").value


def _pane(*, server_identity: str = _SERVER) -> PaneSnapshot:
    return PaneSnapshot(
        server_identity=server_identity,
        pane_id="%7",
        pane_pid=42,
        dead=False,
        executable="python",
        window_id="@3",
        provider_id=None,
        terminal_incarnation=None,
        occupant_id=None,
        occupant_digest=None,
        occupant_pane_pid=None,
        launch_id=None,
        launch_executable=None,
    )


async def test_current_pane_must_match_the_bridge_server(monkeypatch) -> None:
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(bootstrap, "pane_snapshot", lambda _pane_id: _async_value(_pane()))

    assert await bootstrap.require_current_pane(_SERVER) == "@3"


async def test_current_pane_rejects_another_tmux_server(monkeypatch) -> None:
    other = ServerIdentity("/tmp/tmux-other/default", "999", "888").value
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(
        bootstrap,
        "pane_snapshot",
        lambda _pane_id: _async_value(_pane(server_identity=other)),
    )

    with pytest.raises(TmuxError, match="does not match its bridge"):
        await bootstrap.require_current_pane(_SERVER)


async def test_ensure_regie_window_reuses_live_marked_window(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def server_run(socket_path: str, *args: str) -> str:
        calls.append((socket_path, args))
        if args[0] == "display-message":
            return f"{_SOCKET}\t123\t456"
        if args[0] == "set-environment":
            return ""
        if args[0] == "list-windows":
            return "work\t@2\t0\t1"
        raise AssertionError(args)

    monkeypatch.setattr(bootstrap, "_server_run", server_run)

    result = await bootstrap.ensure_regie_window(
        "/project",
        command=("python", "-m", "regie"),
        expected_server_identity=_SERVER,
    )

    assert result == (_SOCKET, "work", "@2")
    assert all(socket == _SOCKET for socket, _args in calls)


async def test_ensure_regie_window_creates_and_marks_on_exact_server(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def server_run(socket_path: str, *args: str) -> str:
        calls.append((socket_path, args))
        if args[0] == "display-message":
            return f"{_SOCKET}\t123\t456"
        if args[0] == "set-environment":
            return ""
        if args[0] == "list-windows":
            return ""
        if args[0] == "list-sessions":
            return "zeta\nalpha"
        if args[0] == "new-window":
            return "@5"
        if args[0] == "set-option":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(bootstrap, "_server_run", server_run)

    assert await bootstrap.ensure_regie_window(
        "/project with spaces",
        command=("python", "-m", "regie"),
        expected_server_identity=_SERVER,
    ) == (_SOCKET, "alpha", "@5")
    new_window = next(args for _socket, args in calls if args[0] == "new-window")
    assert new_window == (
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}",
        "-t",
        "alpha:",
        "-n",
        bootstrap.REGIE_WINDOW_NAME,
        "-c",
        "/project with spaces",
        "--",
        "python",
        "-m",
        "regie",
    )
    assert (
        _SOCKET,
        (
            "set-option",
            "-w",
            "-t",
            "@5",
            bootstrap.REGIE_WINDOW_OPTION,
            bootstrap.REGIE_WINDOW_OPTION_VALUE,
        ),
    ) in calls


async def test_ensure_regie_window_rejects_replaced_server(monkeypatch) -> None:
    async def server_run(_socket_path: str, *args: str) -> str:
        assert args[0] == "display-message"
        return f"{_SOCKET}\t999\t456"

    monkeypatch.setattr(bootstrap, "_server_run", server_run)

    with pytest.raises(TmuxError, match="no longer available"):
        await bootstrap.ensure_regie_window(
            "/project",
            command=("python", "-m", "regie"),
            expected_server_identity=_SERVER,
        )


async def test_color_environment_is_mirrored_without_overwriting_term(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def server_run(_socket_path: str, *args: str) -> str:
        calls.append(args)
        if args[0] == "display-message":
            return f"{_SOCKET}\t123\t456"
        return ""

    monkeypatch.setattr(bootstrap, "_server_run", server_run)
    monkeypatch.setenv("TERM", "xterm-direct")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    await bootstrap.sync_color_environment(_SERVER)

    assert ("set-environment", "-g", "COLORTERM", "truecolor") in calls
    assert ("set-environment", "-g", "NO_COLOR", "") in calls
    assert ("set-environment", "-gu", "FORCE_COLOR") in calls
    assert all("TERM" not in args[2:] for args in calls if args[0] == "set-environment")


def test_launch_selects_window_then_attaches_exact_socket(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []

    async def ensure(*_args, **_kwargs) -> tuple[str, str, str]:
        return _SOCKET, "work", "@5"

    async def server_run(socket_path: str, *args: str) -> str:
        calls.append(("run", socket_path, args))
        return ""

    class ExecCalled(Exception):
        pass

    def execvpe(program: str, argv: list[str], environment: dict[str, str]) -> None:
        calls.append(("exec", program, argv, environment))
        raise ExecCalled

    monkeypatch.setattr(bootstrap, "ensure_regie_window", ensure)
    monkeypatch.setattr(bootstrap, "_server_run", server_run)
    monkeypatch.setenv("TMUX", "stale")
    monkeypatch.setenv("TMUX_PANE", "%999")
    monkeypatch.setattr(bootstrap.os, "execvpe", execvpe)

    with pytest.raises(ExecCalled):
        bootstrap.launch_regie_session(
            "/project",
            command=("python", "-m", "regie"),
            expected_server_identity=_SERVER,
        )

    assert calls[0] == ("run", _SOCKET, ("select-window", "-t", "@5"))
    assert calls[1][0:3] == (
        "exec",
        "tmux",
        ["tmux", "-S", _SOCKET, "attach-session", "-t", "work"],
    )
    environment = calls[1][3]
    assert "TMUX" not in environment
    assert "TMUX_PANE" not in environment


def test_detach_current_client_does_not_mutate_tmux_state(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def run(*args: str, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(bootstrap, "run", run)

    bootstrap.detach_current_client()

    assert calls == [(("detach-client",), {"check": False})]


async def _async_value(value):
    return value
