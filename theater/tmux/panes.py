"""Pane and session operations: queries, mutations, and staging.

The régie's "stage" is a tmux window that shows the currently-selected agent.
Agents themselves live in hidden windows. Swapping the stage occupant means
moving the agent's pane out of its window into the stage window (or vice versa),
without killing anything. tmux's ``break-pane`` and ``join-pane`` do exactly this.

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
Agents park in their own windows (created by ``spawn``), and ``stage`` moves one
onto the stage window as a new pane, or ``unstage`` moves it back.

UNVERIFIED: tmux is unavailable in the development sandbox. argv is asserted
in tests/test_tmux_panes.py. Behaviour must be verified by the user.
"""

from __future__ import annotations

from dataclasses import dataclass

from theater.constants.tmux import (
    TMUX_BREAK_PANE_PLACEHOLDER_NAME,
    TMUX_BREAK_PANE_WORKAROUND_VERSION,
)
from theater.tmux.command import _FORMAT_SEP, _PANE_FORMAT, Pane, TmuxError

_INVENTORY_FORMAT = "#{pid}\t#{pane_id}"


@dataclass(frozen=True, slots=True)
class TmuxInventory:
    """One non-empty tmux server inventory, tied to its server identity."""

    server_identity: str
    pane_ids: frozenset[str]


# Proxies: delegate to the facade at call time so both panes.run and client.run patches work.


async def run(*args: str, check: bool = True) -> str:
    from theater.tmux.client import run as _run

    return await _run(*args, check=check)


def tmux_version() -> str | None:
    from theater.tmux.client import tmux_version as _tmux_version

    return _tmux_version()


# ---- queries -----------------------------------------------------------


async def list_panes(session: str | None = None) -> list[Pane]:
    """Every pane on the server, or only those in one session."""
    # Bare -t is a window target; trailing colon prevents session 0 reading as window 0.
    if session is None:
        scope: list[str] = ["-a"]
    else:
        target = session if session.endswith(":") else f"{session}:"
        scope = ["-s", "-t", target]
    out = await run("list-panes", *scope, "-F", _PANE_FORMAT, check=False)
    return [Pane.parse(line) for line in out.splitlines() if line]


async def observe_inventory() -> TmuxInventory | None:
    """Observe one non-empty server inventory without mixing server epochs."""
    out = await run("list-panes", "-a", "-F", _INVENTORY_FORMAT, check=True)
    rows = [line.partition("\t") for line in out.splitlines() if line]
    if not rows:
        return None
    if any(not separator or not identity or not pane_id for identity, separator, pane_id in rows):
        raise TmuxError("tmux returned an invalid server inventory")
    identities = {identity for identity, _, _ in rows}
    if len(identities) != 1:
        raise TmuxError("tmux returned an invalid server inventory")
    return TmuxInventory(
        server_identity=identities.pop(),
        pane_ids=frozenset(row[2] for row in rows),
    )


async def pane_exists(pane_id: str) -> bool:
    out = await run("list-panes", "-a", "-F", "#{pane_id}", check=False)
    return pane_id in out.split()


async def pane_info(pane_id: str) -> Pane | None:
    """The full row for one pane, or None if it no longer exists.

    `pane_exists` answers the same question more cheaply but only as a
    boolean, and every caller that cares whether a pane is alive also cares
    what is now running in it. One `list-panes` serves both, so asking twice
    would only widen the window between the two answers.
    """
    # Resolve from the facade so conftest patches to client.list_panes are seen.
    from theater.tmux.client import list_panes

    for pane in await list_panes():
        if pane.pane_id == pane_id:
            return pane
    return None


async def sessions() -> list[str]:
    out = await run("list-sessions", "-F", "#{session_name}", check=False)
    return [line for line in out.splitlines() if line]


async def display_message(fmt: str, *, target: str | None = None) -> str:
    """Query a tmux format string for a target pane/window.

    Used by the régie to discover its own pane id and window id at startup:
    `display-message -p -t $TMUX_PANE '#{pane_id}'` and
    `display-message -p -t $TMUX_PANE '#{window_id}'`.
    """
    args = ["display-message", "-p"]
    if target:
        args += ["-t", target]
    args.append(fmt)
    return await run(*args)


# ---- mutations ---------------------------------------------------------


async def ensure_session(name: str, *, cwd: str | None = None) -> str:
    """Create a detached session if it does not exist. Returns the name.

    Only used when Theater has nowhere to put a window: the normal path adopts
    the session the user is already in.
    """
    # Resolve from the facade so conftest patches to client.sessions are seen.
    from theater.tmux.client import sessions

    if name in await sessions():
        return name
    args = ["new-session", "-d", "-s", name]
    if cwd:
        args += ["-c", cwd]
    await run(*args)
    return name


async def new_window(
    *,
    session: str,
    name: str,
    cwd: str,
    command: list[str],
    env: dict[str, str] | None = None,
    background: bool = True,
) -> str:
    """Create a window running `command` and return its pane id.

    `-d` keeps the window from stealing focus. `-P -F` makes tmux print the new
    pane id, which is the whole point: it is how a spawned participant gets an
    identity without any inference.
    """
    target = session if session.endswith(":") else f"{session}:"
    args = ["new-window", "-P", "-F", "#{pane_id}", "-t", target, "-n", name, "-c", cwd]
    if background:
        args.insert(1, "-d")
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    args.append("--")
    args += command
    pane = await run(*args)
    if not pane.startswith("%"):
        raise TmuxError(f"new-window returned an unexpected pane id: {pane!r}")
    return pane


async def kill_pane(pane_id: str) -> None:
    await run("kill-pane", "-t", pane_id, check=False)


# ---- staging -----------------------------------------------------------


async def break_pane(pane_id: str, *, target_window: str | None = None) -> None:
    """Move a pane out of its window into a new window.

    If `target_window` is given, the pane is broken into a new window named
    after it; otherwise tmux chooses the name. Used to park an agent back into
    its own window when unstaging.
    """
    # 3.7 segfaults break-pane without -n; on exactly 3.7 pass -n, capture and rename after.
    is_37 = tmux_version() == TMUX_BREAK_PANE_WORKAROUND_VERSION

    args: list[str] = ["break-pane", "-d", "-s", pane_id]
    if is_37:
        args += [
            "-P",
            "-F",
            "#{window_id}",
            "-n",
            target_window or TMUX_BREAK_PANE_PLACEHOLDER_NAME,
        ]
    elif target_window:
        args += ["-n", target_window]

    result = await run(*args)

    if is_37 and target_window:
        window_id = result.strip()
        await run("rename-window", "-t", window_id, target_window)


async def join_pane(pane_id: str, *, target_window: str, horizontal: bool = True) -> None:
    """Move a pane from its window into another window.

    This is how an agent gets staged: its pane is joined into the stage window.
    `join-pane -d -s <pane> -t <window>` moves it without stealing focus.

    `-h` splits horizontally (side-by-side), which is what the régie wants:
    the agent goes to the right of the sidebar. Without `-h`, tmux defaults
    to a vertical split, stacking the agent below the régie instead.
    """
    args = ["join-pane", "-d"]
    if horizontal:
        args.append("-h")
    args += ["-s", pane_id, "-t", target_window]
    await run(*args)


async def resize_pane(pane_id: str, *, width: int | None = None, height: int | None = None) -> None:
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
    fmt = f"#{{pane_id}}{_FORMAT_SEP}#{{window_id}}"
    out = await run("list-panes", "-a", "-F", fmt, check=False)
    for line in out.splitlines():
        parts = line.split(_FORMAT_SEP)
        if len(parts) == 2 and parts[0] == pane_id:
            return parts[1]
    return None
