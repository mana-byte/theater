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
run against a real server. `kill_pane` and the session-scoped `list_panes`
have not. `deliver_text`'s buffer round-trip was checked against a real
server; its paste into a live TUI is covered end to end by hand.
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


#: Ceiling on a single tmux invocation. tmux commands are local and fast; this
#: only fires when the server is wedged. Named because the daemon socket client
#: derives its own read timeout from it -- a client that gives up before tmux
#: does would desync the connection.
RUN_TIMEOUT = 10.0


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
        ["tmux", *args], capture_output=True, text=True, timeout=RUN_TIMEOUT
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
        out, err = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
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


async def deliver_text(pane_id: str, text: str, *, enter: bool = True) -> None:
    """Insert text into whatever is running in a pane, as a paste.

    Not `send-keys`, which is what this used to be and which is wrong for a
    TUI. `send-keys -l` is literal only in the sense that tmux does not read
    the text as *key names*; the characters still arrive one by one, exactly
    as if a human had typed them, and every keybinding on the far side fires.
    That is not a theoretical problem. OpenCode binds `!` to shell mode, so
    sending

        Hey! Quick fun debate ... over ~10 short dialogue lines

    swallowed the `!`, flipped the composer into shell mode, and the following
    Enter ran the rest of the sentence through zsh -- which answered
    "not enough directory stack entries", because `~10` is a directory stack
    reference. An earlier prompt died on `I'm` with "unmatched '". The agent
    was never prompted at all, so its caller waited for a reply that no one
    was writing. Claude Code binds `!` and a leading `/` the same way; so do
    Codex and Vibe. Escaping cannot fix this: the characters are legitimate
    prose, and the receiving application is right to bind them.

    A paste is the mechanism a terminal already has for "this is text, not
    keystrokes". `paste-buffer -p` wraps the buffer in bracketed-paste markers
    *if the application asked for them* (DECSET 2004) and sends it plain
    otherwise, so tmux makes that decision from the receiver's own declared
    capability rather than from a guess in a table here. All four supported
    CLIs request it; `#{bracket_paste_flag}` reports 1 for each.

    The buffer is named per pane so two concurrent sends cannot paste each
    other's text, and deleted on the way out even if the paste fails, so a
    dead pane cannot leave the buffer stack growing.

    Enter stays a separate `send-keys`: it is a key, and inside a bracketed
    paste it would be inserted as a literal newline instead of submitting.
    """
    buffer = f"theater-{pane_id.lstrip('%')}"
    await run("set-buffer", "-b", buffer, "--", text)
    try:
        await run("paste-buffer", "-b", buffer, "-t", pane_id, "-p", "-d")
    finally:
        # -d already deletes it on success; this is for the failure path.
        await run("delete-buffer", "-b", buffer, check=False)
    if enter:
        await run("send-keys", "-t", pane_id, "Enter")


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


# ---- options -----------------------------------------------------------
#
# Scoped to a session on purpose. `set-option -g` would rewrite the user's
# tmux behaviour for every session on the server and outlive the process that
# did it, which is not a side effect a TUI gets to have. Session scope plus
# `unset_option` on the way out keeps the blast radius to the session the
# régie is actually running in.


async def show_option(name: str, *, target: str) -> str | None:
    """The session-local value of an option, or None if it is not set there.

    Deliberately not `-g`: the question is "did this session override the
    option", because that is what has to be put back afterwards. An unset
    option prints nothing, which is distinguishable from the value "off".
    """
    out = await run("show-options", "-t", target, name, check=False)
    if not out.strip():
        return None
    # Output is "<name> <value>"; anything else means tmux told us something
    # we did not ask about, so treat it as unset rather than guess.
    parts = out.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else None


async def set_option(name: str, value: str, *, target: str) -> None:
    await run("set-option", "-t", target, name, value)


async def unset_option(name: str, *, target: str) -> None:
    """Drop a session-local override so the global value applies again."""
    await run("set-option", "-u", "-t", target, name, check=False)
