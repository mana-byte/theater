"""Launch the Régie UI inside the bridge's exact tmux server."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from typing import NoReturn

from regie.latency import StartupTrace, startup_milestone
from regie.tmux.command import TmuxError, available, run, sequence_argv
from regie.tmux.identity import ServerIdentity, pane_snapshot

REGIE_WINDOW_NAME = "régie"
REGIE_WINDOW_OPTION = "@regie-ui"
REGIE_WINDOW_OPTION_VALUE = "1"
REGIE_PANE_OPTION = "@regie-ui-pane"
REGIE_DEFAULT_SESSION = "theater"

_SERVER_FORMAT = "#{socket_path}\t#{pid}\t#{start_time}"
_PANE_FORMAT = (
    f"#{{session_name}}\t#{{window_id}}\t#{{pane_id}}\t#{{pane_dead}}\t"
    f"#{{{REGIE_WINDOW_OPTION}}}\t#{{{REGIE_PANE_OPTION}}}"
)
_COLOR_ENVIRONMENT = (
    "COLORTERM",
    "NO_COLOR",
    "FORCE_COLOR",
    "CLICOLOR",
    "CLICOLOR_FORCE",
    "COLORFGBG",
    "TERM_PROGRAM",
)


def current_pane_id() -> str | None:
    """Return the invoking tmux pane, if this process is running in one."""
    return os.environ.get("TMUX_PANE") or None


async def require_current_pane(expected_server_identity: str) -> str:
    """Verify that Régie is inside the bridge's pinned tmux server."""
    pane_id = current_pane_id()
    if pane_id is None:
        raise TmuxError("Régie is not running inside a tmux pane")
    snapshot = await pane_snapshot(pane_id)
    if snapshot is None or snapshot.dead or not snapshot.window_id:
        raise TmuxError("Régie's current tmux pane or window cannot be verified")
    if snapshot.server_identity != expected_server_identity:
        expected = ServerIdentity.parse(expected_server_identity)
        actual = ServerIdentity.parse(snapshot.server_identity)
        raise TmuxError(
            "Régie's current tmux server does not match its bridge "
            f"(current {actual.socket_path}, bridge {expected.socket_path}); "
            "run regie outside tmux to attach to the bridge server"
        )
    return snapshot.window_id


async def ensure_regie_window(
    cwd: str,
    *,
    command: Sequence[str],
    expected_server_identity: str,
) -> tuple[str, str, str]:
    """Create or reuse the marked UI window on the bridge's exact server."""
    identity = ServerIdentity.parse(expected_server_identity)
    await _require_server(identity)
    await _mirror_color_environment(identity.socket_path)
    panes = await _server_run(identity.socket_path, "list-panes", "-a", "-F", _PANE_FORMAT)
    for row in panes.splitlines():
        parts = row.split("\t")
        if len(parts) != 6:
            raise TmuxError("tmux returned an invalid Régie pane inventory")
        session, window, pane, pane_dead, marker, marked_pane = parts
        if pane_dead == "0" and marker == REGIE_WINDOW_OPTION_VALUE and marked_pane == pane:
            return identity.socket_path, session, window

    session = await _ensure_default_session(identity.socket_path, cwd)
    created = await _server_run(
        identity.socket_path,
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}\t#{pane_id}",
        "-t",
        f"{session}:",
        "-n",
        REGIE_WINDOW_NAME,
        "-c",
        cwd,
        "--",
        *command,
    )
    window, separator, pane = created.partition("\t")
    if not separator or not window or not pane:
        raise TmuxError("tmux did not identify the new Régie window and pane")
    await _server_run(
        identity.socket_path,
        "set-option",
        "-w",
        "-t",
        window,
        REGIE_WINDOW_OPTION,
        REGIE_WINDOW_OPTION_VALUE,
    )
    await _server_run(
        identity.socket_path,
        "set-option",
        "-w",
        "-t",
        window,
        REGIE_PANE_OPTION,
        pane,
    )
    await _require_server(identity)
    return identity.socket_path, session, window


def launch_regie_session(
    cwd: str,
    *,
    command: Sequence[str],
    expected_server_identity: str,
    startup: StartupTrace | None = None,
) -> NoReturn:
    """Prepare the Régie window and replace this process with a tmux client."""
    socket_path, session, window = asyncio.run(
        ensure_regie_window(
            cwd,
            command=command,
            expected_server_identity=expected_server_identity,
        )
    )
    asyncio.run(_server_run(socket_path, "select-window", "-t", window))
    if startup is not None:
        startup_milestone("attach", startup.started_at)
    environment = os.environ.copy()
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    os.execvpe(
        "tmux",
        ["tmux", "-S", socket_path, "attach-session", "-t", session],
        environment,
    )
    raise RuntimeError("tmux attach unexpectedly returned")


def detach_current_client() -> None:
    """Detach the client displaying this pane without changing any sessions."""
    asyncio.run(run("detach-client", check=False))


async def _require_server(identity: ServerIdentity) -> None:
    output = await _server_run(identity.socket_path, "display-message", "-p", _SERVER_FORMAT)
    parts = output.split("\t")
    if len(parts) != 3 or ServerIdentity(*parts) != identity:
        raise TmuxError("the Régie bridge's pinned tmux server is no longer available")


async def _ensure_default_session(socket_path: str, cwd: str) -> str:
    sessions = (
        await _server_run(socket_path, "list-sessions", "-F", "#{session_name}")
    ).splitlines()
    if REGIE_DEFAULT_SESSION in sessions:
        return REGIE_DEFAULT_SESSION
    try:
        await _server_run(
            socket_path,
            "new-session",
            "-d",
            "-s",
            REGIE_DEFAULT_SESSION,
            "-c",
            cwd,
        )
    except TmuxError:
        sessions = (
            await _server_run(socket_path, "list-sessions", "-F", "#{session_name}")
        ).splitlines()
        if REGIE_DEFAULT_SESSION not in sessions:
            raise
    return REGIE_DEFAULT_SESSION


async def sync_color_environment(expected_server_identity: str) -> None:
    """Make future panes honor the invoking terminal's explicit color hints."""
    identity = ServerIdentity.parse(expected_server_identity)
    await _require_server(identity)
    await _mirror_color_environment(identity.socket_path)


async def live_pane_ids(expected_server_identity: str) -> tuple[str, ...]:
    """Return live pane IDs from the bridge's verified tmux server."""
    identity = ServerIdentity.parse(expected_server_identity)
    await _require_server(identity)
    output = await _server_run(
        identity.socket_path,
        "list-panes",
        "-a",
        "-F",
        "#{pane_id}\t#{pane_dead}",
    )
    panes: list[str] = []
    for row in output.splitlines():
        pane_id, separator, dead = row.partition("\t")
        if not separator or not pane_id.startswith("%") or not pane_id[1:].isdigit():
            raise TmuxError("tmux returned an invalid live pane inventory")
        if dead not in {"0", "1"}:
            raise TmuxError("tmux returned an invalid live pane inventory")
        if dead == "0":
            panes.append(pane_id)
    return tuple(panes)


async def _mirror_color_environment(socket_path: str) -> None:
    # TERM is deliberately absent: tmux supplies its configured terminal type.
    commands: list[tuple[str, ...]] = []
    for name in _COLOR_ENVIRONMENT:
        value = os.environ.get(name)
        if value is None:
            commands.append(("set-environment", "-gu", name))
        else:
            commands.append(("set-environment", "-g", name, value))
    await _server_run(socket_path, *sequence_argv(commands))


async def _server_run(socket_path: str, *args: str) -> str:
    return await run("-S", socket_path, *args)


__all__ = [
    "REGIE_DEFAULT_SESSION",
    "REGIE_PANE_OPTION",
    "REGIE_WINDOW_NAME",
    "REGIE_WINDOW_OPTION",
    "REGIE_WINDOW_OPTION_VALUE",
    "available",
    "current_pane_id",
    "detach_current_client",
    "ensure_regie_window",
    "launch_regie_session",
    "live_pane_ids",
    "require_current_pane",
    "sync_color_environment",
]
