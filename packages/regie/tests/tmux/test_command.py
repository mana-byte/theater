"""Pinned tmux addresses and fail-closed command construction without a real server."""

from __future__ import annotations

import asyncio
import os
import socket as socket_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from regie.tmux import command
from regie.tmux.command import TmuxError, bind_socket, command_argv, resolve_socket

from theater.frontend import SANDBOX_ENV


@pytest.mark.parametrize("kind", ["empty", "missing", "file"])
def test_unusable_temporary_root_never_selects_another_server(tmp_path: Path, kind: str) -> None:
    root = tmp_path / kind
    if kind == "file":
        root.touch()
    env = {"TMUX_TMPDIR": "" if kind == "empty" else str(root), "TMUX": "/tmp/default,1,0"}
    with pytest.raises(TmuxError, match="TMUX_TMPDIR"):
        resolve_socket(environment=env)


def test_default_and_inherited_socket_selection_preserve_tmux_conventions(tmp_path: Path) -> None:
    default = resolve_socket(environment={})
    assert default.path == Path("/tmp").resolve() / f"tmux-{os.getuid()}" / "default"
    custom = tmp_path / "custom.sock"
    inherited = resolve_socket(environment={"TMUX": f"{custom},12,0", "TMUX_TMPDIR": str(tmp_path)})
    assert inherited.path == custom
    assert not inherited.default_directory


@pytest.mark.parametrize("case", ["missing_home", "missing_tmpdir", "outside", "inherited"])
def test_sandbox_requires_a_socket_in_its_private_home_or_state(tmp_path: Path, case: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {SANDBOX_ENV: "test-token", "HOME": str(home), "TMUX_TMPDIR": str(home)}
    if case == "missing_home":
        del env["HOME"]
    elif case == "missing_tmpdir":
        del env["TMUX_TMPDIR"]
    elif case == "outside":
        env["TMUX_TMPDIR"] = str(tmp_path)
    else:
        env["TMUX"] = f"{tmp_path / 'default.sock'},1,0"
    with pytest.raises(TmuxError, match="sandbox"):
        resolve_socket(environment=env, state_dir=home / "state")


@pytest.mark.parametrize("location", ["home", "state"])
def test_sandbox_accepts_its_configured_private_socket(tmp_path: Path, location: str) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir()
    state.mkdir()
    env = {SANDBOX_ENV: "test-token", "HOME": str(home), "TMUX_TMPDIR": str(home)}
    path = (home if location == "home" else state) / "custom.sock"
    socket = resolve_socket(environment=env, state_dir=state, socket_path=path)
    socket.prepare()
    with bind_socket(socket):
        assert command_argv("list-sessions") == ("tmux", "-S", str(path), "list-sessions")


async def test_unbound_commands_fail_before_starting_a_subprocess(monkeypatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("a tmux subprocess must not start")

    monkeypatch.setattr(command, "available", lambda: True)
    monkeypatch.setattr(command.asyncio, "create_subprocess_exec", unexpected)
    with pytest.raises(TmuxError, match="pinned"):
        await command.run("kill-server", check=False)
    with pytest.raises(TmuxError, match="empty"):
        await command.run("-S", "", "kill-server", check=False)


async def test_child_tasks_keep_the_socket_when_environment_and_parent_context_change(
    tmp_path: Path, monkeypatch
) -> None:
    socket = resolve_socket(environment={"TMUX_TMPDIR": str(tmp_path)})
    socket.prepare()
    calls = []

    async def communicate(_input):
        return b"verified\n", b""

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(command, "available", lambda: True)
    monkeypatch.setattr(command.asyncio, "create_subprocess_exec", create)
    with bind_socket(socket):
        task = asyncio.create_task(command.run("display-message", "-p", "#{socket_path}"))
        assert command_argv("-V") == ("tmux", "-S", str(socket.path), "-V")
        for args in (("-L", "other", "kill-server"), ("-S", "/tmp/other", "kill-server")):
            with pytest.raises(TmuxError, match="pinned"):
                command_argv(*args)
    monkeypatch.setenv("TMUX", "/tmp/user-default,1,0")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "missing"))
    assert await task == "verified"
    assert calls[0][0] == (
        "tmux",
        "-S",
        str(socket.path),
        "display-message",
        "-p",
        "#{socket_path}",
    )
    with pytest.raises(TmuxError, match="pinned"):
        command_argv("kill-server")


@pytest.mark.parametrize("case", ["default", "sandbox", "explicit", "existing", "version"])
async def test_only_new_ordinary_default_sockets_gain_group_permissions(
    tmp_path: Path, monkeypatch, case: str
) -> None:
    env = {"TMUX_TMPDIR": str(tmp_path)}
    if case == "sandbox":
        env.update({SANDBOX_ENV: "test-token", "HOME": str(tmp_path)})
    socket = resolve_socket(
        environment=env, socket_path=tmp_path / "custom.sock" if case == "explicit" else None
    )
    socket.prepare()
    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)

    async def communicate(_input):
        return b"ok\n", b""

    async def create(*_args, **_kwargs):
        if case not in {"existing", "version"}:
            server.bind(str(socket.path))
            socket.path.chmod(0o600)
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(command, "available", lambda: True)
    monkeypatch.setattr(command.asyncio, "create_subprocess_exec", create)
    try:
        if case == "existing":
            server.bind(str(socket.path))
            socket.path.chmod(0o620)
        with bind_socket(socket):
            assert await command.run("-V" if case == "version" else "new-session") == "ok"
        if case == "version":
            assert not socket.path.exists()
        else:
            expected = 0o660 if case == "default" else 0o620 if case == "existing" else 0o600
            assert socket.path.stat().st_mode & 0o777 == expected
    finally:
        server.close()


@pytest.mark.parametrize("change", ["permissions", "owner", "symlink"])
def test_default_socket_directory_is_private_and_cannot_be_redirected(
    tmp_path: Path, monkeypatch, change: str
) -> None:
    socket = resolve_socket(environment={"TMUX_TMPDIR": str(tmp_path)})
    socket.prepare()
    assert socket.path.parent.stat().st_mode & 0o777 == 0o700
    if change == "permissions":
        socket.path.parent.chmod(0o707)
    elif change == "owner":
        monkeypatch.setattr(command.os, "getuid", lambda: socket.path.parent.stat().st_uid + 1)
    else:
        socket.path.parent.rmdir()
        socket.path.parent.symlink_to(tmp_path, target_is_directory=True)
    with bind_socket(socket), pytest.raises(TmuxError):
        command_argv("kill-server")
