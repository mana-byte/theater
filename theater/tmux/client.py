"""Thin subprocess wrappers around tmux.

Everything Theater knows about tmux goes through here so the surface stays small
and mockable. tmux is a hard dependency; if it is missing we fail loudly rather
than degrading, because there is no inbound delivery path without it.

Targets are always written `session:`, never bare
-----------------------------------------------
tmux resolves a bare `-t 0` as *window index 0*, not as the session named `0`,
and unnamed sessions are named by number — so on a default setup `new-window -t 0`
means "create at index 0" and fails with "index 0 in use". The trailing colon
makes it a session target and lets tmux choose the index. This cost a real
spawn failure; the argv is now asserted in tests/test_tmux_client.py, because
argv can be checked without a tmux server and the behaviour cannot.

PARTLY VERIFIED. `ensure_session`, `new_window` and pane-id capture have been
run against a real server. `send_keys`, `kill_pane` and the session-scoped
`list_panes` have not.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass

from theater.models import TheaterError

# Fields we ask tmux for when enumerating panes, in order.
_PANE_FORMAT = "#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{window_id}\t#{session_name}\t#{window_name}\t#{pane_current_command}"


class TmuxError(TheaterError):
    code = "tmux_error"


class TmuxMissing(TmuxError):
    code = "tmux_missing"


@dataclass(frozen=True, slots=True)
class Pane:
    pane_id: str
    pane_pid: int
    cwd: str
    window_id: str
    session: str
    window_name: str
    current_command: str

    @classmethod
    def parse(cls, line: str) -> Pane:
        parts = line.split("\t")
        if len(parts) != 7:
            raise TmuxError(f"unexpected list-panes row: {line!r}")
        return cls(
            pane_id=parts[0],
            pane_pid=int(parts[1]),
            cwd=parts[2],
            window_id=parts[3],
            session=parts[4],
            window_name=parts[5],
            current_command=parts[6],
        )


def available() -> bool:
    return shutil.which("tmux") is not None


def inside_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def current_pane() -> str | None:
    """The pane of the *calling* process, if it is itself inside tmux."""
    return os.environ.get("TMUX_PANE")


def _require() -> None:
    if not available():
        raise TmuxMissing("tmux is not on PATH; Theater cannot run without it")


def run_sync(*args: str, check: bool = True) -> str:
    _require()
    proc = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=10
    )
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


async def run(*args: str, check: bool = True) -> str:
    _require()
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        proc.kill()
        raise TmuxError(f"tmux {' '.join(args)} timed out") from None
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)} failed: {err.decode().strip()}")
    return out.decode().rstrip("\n")


# ---- queries -----------------------------------------------------------


async def list_panes(session: str | None = None) -> list[Pane]:
    """Every pane on the server, or only those in one session."""
    # A bare `-t <name>` is a window target; session scope needs `-s -t <name>`.
    # `name` here is a session name, so also append the trailing colon that
    # disambiguates `0` (the unnamed session) from window index 0.
    if session is None:
        scope: list[str] = ["-a"]
    else:
        target = session if session.endswith(":") else f"{session}:"
        scope = ["-s", "-t", target]
    out = await run("list-panes", *scope, "-F", _PANE_FORMAT, check=False)
    return [Pane.parse(line) for line in out.splitlines() if line]


async def pane_exists(pane_id: str) -> bool:
    out = await run(
        "list-panes", "-a", "-F", "#{pane_id}", check=False
    )
    return pane_id in out.split()


async def sessions() -> list[str]:
    out = await run("list-sessions", "-F", "#{session_name}", check=False)
    return [line for line in out.splitlines() if line]


def current_session_sync() -> str | None:
    """Session name of the calling process, or None if not inside tmux."""
    if not inside_tmux() or not available():
        return None
    try:
        return run_sync("display-message", "-p", "#{session_name}") or None
    except TmuxError:
        return None


# ---- mutations ---------------------------------------------------------


async def ensure_session(name: str, *, cwd: str | None = None) -> str:
    """Create a detached session if it does not exist. Returns the name.

    Only used when Theater has nowhere to put a window: the normal path adopts
    the session the user is already in.
    """
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


async def send_keys(pane_id: str, text: str, *, enter: bool = True) -> None:
    """Phase 5b uses this for real. Present here only so the wrapper set is whole.

    `-l` sends the text literally, so prompt content is never interpreted as a
    key name. The Enter is a separate call for the same reason.
    """
    await run("send-keys", "-t", pane_id, "-l", "--", text)
    if enter:
        await run("send-keys", "-t", pane_id, "Enter")
