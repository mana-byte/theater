"""Bare CLI launch and tmux bootstrap behaviour."""

from __future__ import annotations

import sys

import pytest

from theater import cli
from theater.cli.commands import launch as launch_mod
from theater.cli.errors import BadUsage
from theater.constants.tmux import (
    TMUX_DEFAULT_SESSION,
    TMUX_REGIE_WINDOW_NAME,
    TMUX_REGIE_WINDOW_OPTION,
    TMUX_REGIE_WINDOW_OPTION_VALUE,
)
from theater.tmux import bootstrap


def test_main_dispatches_no_subcommand_to_launcher(monkeypatch):
    seen = []
    monkeypatch.setitem(cli._COMMANDS, None, lambda args: seen.append(args.command) or 7)
    monkeypatch.setattr(cli.config, "load", object)
    monkeypatch.setattr(cli.harness_registry, "install", lambda settings: None)

    assert cli.main([]) == 7
    assert seen == [None]


def test_bare_launch_inside_tmux_starts_daemon_then_runs_regie(monkeypatch):
    calls = []
    monkeypatch.setattr(launch_mod.tmux, "available", lambda: True)
    monkeypatch.setattr(launch_mod.tmux, "inside_tmux", lambda: True)
    monkeypatch.setattr(launch_mod, "call_sync", lambda method: calls.append(("rpc", method)))
    monkeypatch.setattr(launch_mod, "cmd_regie", lambda args: calls.append(("regie", args)) or 9)
    monkeypatch.setattr(
        launch_mod,
        "detach_current_client",
        lambda: calls.append(("detach",)),
    )
    monkeypatch.setattr(
        launch_mod,
        "launch_regie_session",
        lambda cwd: (_ for _ in ()).throw(AssertionError("nested tmux bootstrap")),
    )
    args = object()

    assert launch_mod.cmd_launch(args) == 9
    assert calls == [("rpc", "ping"), ("regie", args)]


def test_bare_launch_inside_tmux_detaches_after_regie_quits(monkeypatch):
    calls = []
    monkeypatch.setattr(launch_mod.tmux, "available", lambda: True)
    monkeypatch.setattr(launch_mod.tmux, "inside_tmux", lambda: True)
    monkeypatch.setattr(launch_mod, "call_sync", lambda method: calls.append(("rpc", method)))
    monkeypatch.setattr(launch_mod, "cmd_regie", lambda args: calls.append(("regie", args)) or 0)
    monkeypatch.setattr(
        launch_mod,
        "detach_current_client",
        lambda: calls.append(("detach",)),
    )
    args = object()

    assert launch_mod.cmd_launch(args) == 0
    assert calls == [("rpc", "ping"), ("regie", args), ("detach",)]


def test_bare_launch_outside_tmux_delegates_after_daemon_preflight(monkeypatch):
    calls = []
    monkeypatch.setattr(launch_mod.tmux, "available", lambda: True)
    monkeypatch.setattr(launch_mod.tmux, "inside_tmux", lambda: False)
    monkeypatch.setattr(launch_mod, "call_sync", lambda method: calls.append(("rpc", method)))
    monkeypatch.setattr(
        launch_mod,
        "launch_regie_session",
        lambda cwd: calls.append(("tmux", cwd)),
    )

    assert launch_mod.cmd_launch(object()) == 0
    assert calls[0] == ("rpc", "ping")
    assert calls[1][0] == "tmux"


def test_bare_launch_refuses_without_tmux_before_starting_daemon(monkeypatch):
    called = []
    monkeypatch.setattr(launch_mod.tmux, "available", lambda: False)

    def call_sync(method):
        called.append(method)

    monkeypatch.setattr(launch_mod, "call_sync", call_sync)

    with pytest.raises(BadUsage, match="tmux is not on PATH"):
        launch_mod.cmd_launch(object())
    assert called == []


def test_bare_launch_turns_tmux_failure_into_actionable_cli_error(monkeypatch, capsys):
    monkeypatch.setattr(launch_mod.tmux, "available", lambda: True)
    monkeypatch.setattr(launch_mod.tmux, "inside_tmux", lambda: False)
    monkeypatch.setattr(launch_mod, "call_sync", lambda method: None)
    monkeypatch.setattr(cli.config, "load", object)
    monkeypatch.setattr(cli.harness_registry, "install", lambda settings: None)

    def fail(cwd):
        raise launch_mod.tmux.TmuxError("new-session failed")

    monkeypatch.setattr(launch_mod, "launch_regie_session", fail)

    assert cli.main([]) == 1
    error = capsys.readouterr().err
    assert "check tmux and retry" in error
    assert "Traceback" not in error


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
