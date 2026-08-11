"""Staging: move panes between windows without destroying them.

The régie's "stage" is a tmux window that shows the currently-selected agent.
Agents themselves live in hidden windows. Swapping the stage occupant means
moving the agent's pane out of its window into the stage window (or vice versa),
without killing anything. tmux's `break-pane` and `join-pane` do exactly this.

Layout target
-------------
    ┌──────────────┬────────────────────────────┐
    │ régie        │  stage                     │
    │  (ours)      │  (real tmux pane)          │
    │ ▾ vibe#1     │                            │
    │   ├ claude#2 │   the selected agent,      │
    │   └ claude#3 │   fully interactive        │
    │ ── bus ───── │                            │
    │ #1 → #3 send │                            │
    └──────────────┴────────────────────────────┘

The régie is a pane in the same session as the stage. The stage is a window.
Agents park in their own windows (created by `spawn`), and `stage` moves one
onto the stage window as a new pane, or `unstage` moves it back.

UNVERIFIED: tmux is unavailable in the development sandbox. argv is asserted
in tests/test_tmux_panes.py. Behaviour must be verified by the user.
"""

from __future__ import annotations

from theater.tmux.client import TmuxError, run


async def break_pane(pane_id: str, *, target_window: str | None = None) -> None:
    """Move a pane out of its window into a new window.

    If `target_window` is given, the pane is broken into a new window named
    after it; otherwise tmux chooses the name. Used to park an agent back into
    its own window when unstaging.
    """
    args = ["break-pane", "-d", "-s", pane_id]
    if target_window:
        args += ["-n", target_window]
    await run(*args)


async def join_pane(pane_id: str, *, target_window: str) -> None:
    """Move a pane from its window into another window.

    This is how an agent gets staged: its pane is joined into the stage window.
    `join-pane -d -s <pane> -t <window>` moves it without stealing focus.
    """
    await run("join-pane", "-d", "-s", pane_id, "-t", target_window)


async def resize_pane(
    pane_id: str, *, width: int | None = None, height: int | None = None
) -> None:
    """Resize a pane to exact dimensions.

    Used after staging so the agent renders for the stage's width, not whatever
    size its hidden window had.
    """
    if width is not None:
        await run("resize-pane", "-t", pane_id, "-x", str(width))
    if height is not None:
        await run("resize-pane", "-t", pane_id, "-y", str(height))


async def select_pane(pane_id: str) -> None:
    """Focus a pane. Used when the user wants to type at the staged agent."""
    await run("select-pane", "-t", pane_id, check=False)


async def kill_window(window_id: str) -> None:
    """Kill an entire window and all its panes."""
    await run("kill-window", "-t", window_id, check=False)


async def split_window(
    *,
    target: str,
    cwd: str | None = None,
    command: list[str] | None = None,
    vertical: bool = True,
    background: bool = True,
) -> str:
    """Split a window to create a new pane, returning the new pane id.

    Used to create the régie pane next to the stage pane inside the same
    window. `-P -F #{pane_id}` captures the new pane's id, same trick as
    `new_window`.
    """
    args = ["split-window", "-P", "-F", "#{pane_id}", "-t", target]
    if vertical:
        args.insert(1, "-v")
    else:
        args.insert(1, "-h")
    if background:
        args.insert(1, "-d")
    if cwd:
        args += ["-c", cwd]
    if command:
        args.append("--")
        args += command
    pane = await run(*args)
    if not pane.startswith("%"):
        raise TmuxError(f"split-window returned unexpected pane id: {pane!r}")
    return pane


async def new_window_named(
    *,
    session: str,
    name: str,
    cwd: str | None = None,
    command: list[str] | None = None,
    background: bool = True,
) -> str:
    """Create a named window and return its pane id.

    Thin wrapper around `new-window` that does not take an env dict — used by
    the régie to create the stage window itself.
    """
    target = session if session.endswith(":") else f"{session}:"
    args = ["new-window", "-P", "-F", "#{pane_id}", "-t", target, "-n", name]
    if background:
        args.insert(1, "-d")
    if cwd:
        args += ["-c", cwd]
    if command:
        args.append("--")
        args += command
    pane = await run(*args)
    if not pane.startswith("%"):
        raise TmuxError(f"new-window returned unexpected pane id: {pane!r}")
    return pane


async def swap_panes(src: str, dst: str) -> None:
    """Swap two panes' positions. Used for zoom-style full-screen staging."""
    await run("swap-pane", "-s", src, "-t", dst, check=False)


async def move_window_to_index(window_id: str, index: int, *, session: str) -> None:
    """Move a window to a specific index in a session."""
    target = session if session.endswith(":") else f"{session}:"
    await run("move-window", "-s", window_id, "-t", f"{target}{index}", check=False)


async def window_for_pane(pane_id: str) -> str | None:
    """Which window id does a pane belong to?"""
    out = await run(
        "list-panes", "-a", "-F", "#{pane_id}\t#{window_id}", check=False
    )
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] == pane_id:
            return parts[1]
    return None
