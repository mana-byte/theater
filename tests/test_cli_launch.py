"""Legacy CLI migration guidance and retained tmux bootstrap behaviour."""

from __future__ import annotations

import sys

import pytest

from theater import cli
from theater.cli.commands import launch as launch_mod
from theater.constants.tmux import (
    TMUX_DEFAULT_SESSION,
    TMUX_REGIE_WINDOW_NAME,
    TMUX_REGIE_WINDOW_OPTION,
    TMUX_REGIE_WINDOW_OPTION_VALUE,
)
from theater.tmux import bootstrap


def test_main_without_a_subcommand_never_dispatches_the_legacy_launcher(monkeypatch, capsys):
    monkeypatch.setitem(
        cli._COMMANDS,
        None,
        lambda _args: (_ for _ in ()).throw(AssertionError("legacy launcher was dispatched")),
    )

    assert cli.main([]) == 0
    assert "standalone `regie`" in capsys.readouterr().out


def test_legacy_launch_helper_only_gives_standalone_guidance(capsys):
    assert launch_mod.cmd_launch(object()) == 0
    assert "standalone `regie`" in capsys.readouterr().out


async def test_bootstrap_creates_and_marks_missing_regie_window(monkeypatch):
    calls = []

    async def ensure_session(name, *, cwd=None):
        calls.append(("ensure-session", name, cwd))
        return name

    async def run(*args, check=True):
        calls.append(("run", args, check))
        return ""

    async def new_window_named(**kwargs):
        calls.append(("new-window", kwargs))
        return "%9"

    async def display_message(fmt, *, target=None):
        calls.append(("display", fmt, target))
        return "@4"

    async def set_window_option(name, value, *, target):
        calls.append(("mark", name, value, target))

    monkeypatch.setattr(bootstrap.tmux, "run", run)
    monkeypatch.setattr(bootstrap.tmux, "ensure_session", ensure_session)
    monkeypatch.setattr(bootstrap.tmux, "new_window_named", new_window_named)
    monkeypatch.setattr(bootstrap.tmux, "display_message", display_message)
    monkeypatch.setattr(bootstrap.tmux, "set_window_option", set_window_option)

    assert await bootstrap.ensure_regie_window("/project") == (TMUX_DEFAULT_SESSION, "@4")
    assert calls[0] == ("ensure-session", TMUX_DEFAULT_SESSION, "/project")
    created = next(call[1] for call in calls if call[0] == "new-window")
    assert created == {
        "session": TMUX_DEFAULT_SESSION,
        "name": TMUX_REGIE_WINDOW_NAME,
        "cwd": "/project",
        "command": [sys.executable, "-m", "theater.cli"],
    }
    assert ("mark", TMUX_REGIE_WINDOW_OPTION, TMUX_REGIE_WINDOW_OPTION_VALUE, "@4") in calls


async def test_bootstrap_reuses_live_marked_regie_window(monkeypatch):
    async def ensure_session(name, *, cwd=None):
        return name

    async def run(*args, check=True):
        if args[0] == "list-windows":
            return "@2\t0\n@3\t0"
        return ""

    async def show_window_option(name, *, target):
        return TMUX_REGIE_WINDOW_OPTION_VALUE if target == "@3" else None

    async def unexpected(**kwargs):
        raise AssertionError("a second régie window was created")

    monkeypatch.setattr(bootstrap.tmux, "run", run)
    monkeypatch.setattr(bootstrap.tmux, "ensure_session", ensure_session)
    monkeypatch.setattr(bootstrap.tmux, "show_window_option", show_window_option)
    monkeypatch.setattr(bootstrap.tmux, "new_window_named", unexpected)

    assert await bootstrap.ensure_regie_window("/project") == (TMUX_DEFAULT_SESSION, "@3")


async def test_bootstrap_accepts_a_concurrent_session_creator(monkeypatch):
    async def ensure_session(name, *, cwd=None):
        raise bootstrap.tmux.TmuxError("duplicate session")

    async def sessions():
        return [TMUX_DEFAULT_SESSION]

    async def run(*args, check=True):
        return "@3\t0" if args[0] == "list-windows" else ""

    async def show_window_option(name, *, target):
        return TMUX_REGIE_WINDOW_OPTION_VALUE

    monkeypatch.setattr(bootstrap.tmux, "ensure_session", ensure_session)
    monkeypatch.setattr(bootstrap.tmux, "sessions", sessions)
    monkeypatch.setattr(bootstrap.tmux, "run", run)
    monkeypatch.setattr(bootstrap.tmux, "show_window_option", show_window_option)

    assert await bootstrap.ensure_regie_window("/project") == (TMUX_DEFAULT_SESSION, "@3")


async def test_bootstrap_preserves_a_real_session_creation_error(monkeypatch):
    error = bootstrap.tmux.TmuxError("permission denied")

    async def ensure_session(name, *, cwd=None):
        raise error

    async def sessions():
        return []

    monkeypatch.setattr(bootstrap.tmux, "ensure_session", ensure_session)
    monkeypatch.setattr(bootstrap.tmux, "sessions", sessions)

    with pytest.raises(bootstrap.tmux.TmuxError) as caught:
        await bootstrap.ensure_regie_window("/project")
    assert caught.value is error


def test_attach_selects_regie_then_execs_tmux(monkeypatch):
    calls = []

    class ExecCalled(Exception):
        pass

    monkeypatch.setattr(bootstrap.tmux, "run_sync", lambda *args: calls.append(("select", args)))

    def execvp(program, argv):
        calls.append(("exec", program, argv))
        raise ExecCalled

    monkeypatch.setattr(bootstrap.os, "execvp", execvp)

    with pytest.raises(ExecCalled):
        bootstrap.attach_regie("theater", "@4")
    assert calls == [
        ("select", ("select-window", "-t", "@4")),
        ("exec", "tmux", ["tmux", "attach-session", "-t", "theater"]),
    ]


def test_detach_current_client_preserves_tmux_state(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bootstrap.tmux,
        "run_sync",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bootstrap.detach_current_client()

    assert calls == [(("detach-client",), {"check": False})]
