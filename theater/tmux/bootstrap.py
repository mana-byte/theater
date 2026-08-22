"""Create or reuse the tmux window that hosts the régie."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import NoReturn

from theater.constants.tmux import (
    TMUX_DEFAULT_SESSION,
    TMUX_REGIE_WINDOW_NAME,
    TMUX_REGIE_WINDOW_OPTION,
    TMUX_REGIE_WINDOW_OPTION_VALUE,
)
from theater.tmux import client as tmux


async def _marked_regie_window(session: str) -> str | None:
    target = session if session.endswith(":") else f"{session}:"
    rows = await tmux.run(
        "list-windows",
        "-t",
        target,
        "-F",
        "#{window_id}\t#{pane_dead}",
        check=False,
    )
    for row in rows.splitlines():
        window, separator, pane_dead = row.partition("\t")
        if not separator or pane_dead == "1":
            continue
        marker = await tmux.show_window_option(TMUX_REGIE_WINDOW_OPTION, target=window)
        if marker == TMUX_REGIE_WINDOW_OPTION_VALUE:
            return window
    return None


async def ensure_regie_window(cwd: str, *, command: list[str] | None = None) -> tuple[str, str]:
    """Ensure the shared session and its singleton régie window exist."""
    session = TMUX_DEFAULT_SESSION
    try:
        await tmux.ensure_session(session, cwd=cwd)
    except tmux.TmuxError:
        if session not in await tmux.sessions():
            raise
    if window := await _marked_regie_window(session):
        return session, window

    pane = await tmux.new_window_named(
        session=session,
        name=TMUX_REGIE_WINDOW_NAME,
        cwd=cwd,
        command=command or [sys.executable, "-m", "theater.cli"],
    )
    window = await tmux.display_message("#{window_id}", target=pane)
    await tmux.set_window_option(
        TMUX_REGIE_WINDOW_OPTION,
        TMUX_REGIE_WINDOW_OPTION_VALUE,
        target=window,
    )
    return session, window


def attach_regie(session: str, window: str) -> NoReturn:
    """Select the régie window and replace this process with a tmux client."""
    tmux.run_sync("select-window", "-t", window)
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    raise RuntimeError("tmux attach unexpectedly returned")


def launch_regie_session(cwd: str) -> NoReturn:
    """Prepare the régie window, then attach the invoking terminal to it."""
    session, window = asyncio.run(ensure_regie_window(cwd))
    attach_regie(session, window)


def detach_current_client() -> None:
    """Detach this tmux client without changing the session or participant panes."""
    tmux.run_sync("detach-client", check=False)
