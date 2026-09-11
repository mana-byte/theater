"""Human-presence facts for a tmux server: focus inventory, wake hooks, waiter.

Copy mode stays the pane-local signal (§10): a wrong "no human" injects
keystrokes into a pane a human is using, which is unrecoverable.  Query
errors now propagate instead of reading as "no human" — fail-closed is the
caller's job, not this module's.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass

from theater.constants.presence import (
    PRESENCE_FEATURE_FOCUS,
    PRESENCE_FLAG_FOCUSED,
    PRESENCE_FOCUS_EVENTS_OPTION,
    PRESENCE_WAKE_CHANNEL,
    PRESENCE_WAKE_HOOK_EVENTS,
)
from theater.constants.tmux import TMUX_FIELD_SEPARATOR
from theater.tmux.command import TmuxError
from theater.tmux.panes import TmuxServerIdentity

_SEP = TMUX_FIELD_SEPARATOR
_HOOK_INDEX = re.compile(r"^\w[\w-]*\[(\d+)\] (.*)$")

# window_id rides along: presence protects the whole displayed window when a
# client's independent active pane cannot be observed.
_IDENTITY = "#{socket_path}\t#{pid}\t#{start_time}"
_FOCUS_PANE_FORMAT = f"#{{pane_id}}{_SEP}#{{window_id}}{_SEP}{_IDENTITY}"
_FOCUS_CLIENT_FORMAT = _SEP.join(
    (
        "#{client_tty}",
        "#{client_pid}",
        "#{client_created}",
        "#{client_flags}",
        "#{client_readonly}",
        "#{client_control_mode}",
        "#{window_id}",
        "#{pane_id}",
        "#{client_termfeatures}",
    )
)


# Proxy: delegate to client.run at call time so both presence.run and client.run patches work.
async def run(*args: str, check: bool = True) -> str:
    from theater.tmux.client import run as _run

    return await _run(*args, check=check)


async def human_present(pane_id: str) -> bool:
    """Is a human likely present at this pane via copy mode?

    Query errors propagate: a failed pane query must never read as "absent".
    """
    in_mode = await run("display-message", "-p", "-t", pane_id, "#{pane_in_mode}")
    return bool(in_mode and in_mode != "0")


# ---- focus inventory ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class FocusClient:
    """One attached terminal client; (pid, created) is its lifetime identity."""

    tty: str
    pid: str
    created: str
    flags: frozenset[str]
    readonly: bool
    control: bool
    window_id: str
    active_pane_id: str
    termfeatures: frozenset[str]

    @property
    def identity(self) -> tuple[str, str]:
        return (self.pid, self.created)

    @property
    def focused(self) -> bool:
        # CLIENT_FOCUSED defaults on: a fresh attach reads focused until a real
        # focus-out arrives, which protects conservatively.
        return PRESENCE_FLAG_FOCUSED in self.flags

    @property
    def input_capable(self) -> bool:
        return not self.readonly and not self.control

    @property
    def focus_reporting(self) -> bool:
        return PRESENCE_FEATURE_FOCUS in self.termfeatures


@dataclass(frozen=True, slots=True)
class FocusInventory:
    """One complete server inventory: panes, clients, and the server epoch."""

    server_identity: str
    panes: dict[str, str]
    clients: tuple[FocusClient, ...]
    observed_at: float


def parse_focus_inventory(pane_out: str, client_out: str, *, observed_at: float) -> FocusInventory:
    """Strictly parse `list-panes`/`list-clients` output; malformed rows raise."""
    identities: set[str] = set()
    panes: dict[str, str] = {}
    for line in pane_out.splitlines():
        if not line:
            continue
        parts = line.split(_SEP)
        if len(parts) != 3:
            raise TmuxError(f"unexpected focus pane row: {line!r}")
        pane_id, window_id, identity = parts
        if not pane_id or not window_id or "\t" not in identity:
            raise TmuxError(f"unexpected focus pane row: {line!r}")
        identities.add(TmuxServerIdentity(*identity.split("\t")).value)
        panes[pane_id] = window_id
    if len(identities) > 1:
        raise TmuxError("tmux returned a mixed-server focus inventory")
    if not identities:
        return FocusInventory("", {}, (), observed_at)
    clients: list[FocusClient] = []
    for line in client_out.splitlines():
        if not line:
            continue
        parts = line.split(_SEP)
        if len(parts) != 9 or not all(parts):
            raise TmuxError(f"unexpected focus client row: {line!r}")
        clients.append(
            FocusClient(
                tty=parts[0],
                pid=parts[1],
                created=parts[2],
                flags=frozenset(f for f in parts[3].split(",") if f),
                readonly=parts[4] == "1",
                control=parts[5] == "1",
                window_id=parts[6],
                active_pane_id=parts[7],
                termfeatures=frozenset(f for f in parts[8].split(",") if f),
            )
        )
    return FocusInventory(identities.pop(), panes, tuple(clients), observed_at)


async def observe_focus_inventory(*, clock=None) -> FocusInventory:
    """Observe panes, clients, and server epoch in two bounded tmux calls."""
    import time

    observed_at = clock() if clock is not None else time.time()
    pane_out = await run("list-panes", "-a", "-F", _FOCUS_PANE_FORMAT, check=True)
    # No attached clients is an empty rc-0 listing, not an error.
    client_out = await run("list-clients", "-F", _FOCUS_CLIENT_FORMAT, check=True)
    return parse_focus_inventory(pane_out, client_out, observed_at=observed_at)


# ---- focus events ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FocusEventsStatus:
    """Result of ensuring the focus-events option; drives reattach diagnosis."""

    enabled: bool
    previously_off: bool
    focusless_clients: tuple[str, ...]


async def ensure_focus_events() -> FocusEventsStatus:
    """Turn focus-events on globally and report clients that cannot report focus.

    Existing clients must not be detached to pick the option up — the daemon
    diagnoses, the human reattaches.
    """
    current = await run("show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION, check=False)
    previously_off = current.strip() != "on"
    if previously_off:
        await run("set-option", "-g", PRESENCE_FOCUS_EVENTS_OPTION, "on")
    client_out = await run(
        "list-clients",
        "-F",
        f"#{{client_tty}}{_SEP}#{{client_termfeatures}}",
        check=False,
    )
    focusless = []
    for line in client_out.splitlines():
        tty, _, features = line.partition(_SEP)
        if tty and PRESENCE_FEATURE_FOCUS not in features.split(","):
            focusless.append(tty)
    return FocusEventsStatus(True, previously_off, tuple(focusless))


# ---- wake hooks and waiter ---------------------------------------------


def wake_command(channel: str = PRESENCE_WAKE_CHANNEL) -> str:
    """The hook body: a tmux-internal command, no subprocess per firing."""
    return f"wait-for -S {channel}"


async def _hook_entries(scope: tuple[str, ...], event: str) -> dict[int, str]:
    out = await run("show-hooks", *scope, event, check=False)
    entries: dict[int, str] = {}
    for line in out.splitlines():
        match = _HOOK_INDEX.match(line)
        if match is None:
            continue
        try:
            entries[int(match.group(1))] = match.group(2)
        except ValueError:
            continue
    return entries


async def _sessions() -> list[str]:
    from theater.tmux.client import sessions as _sessions_fn

    return list(await _sessions_fn())


async def _sweep_ours(scope: tuple[str, ...], event: str, command: str) -> None:
    for index, body in (await _hook_entries(scope, event)).items():
        if body == command:
            await run("set-hook", *scope, "-u", f"{event}[{index}]", check=False)


async def install_focus_wake_hooks(channel: str = PRESENCE_WAKE_CHANNEL) -> list[str]:
    """Append one owned wake entry per event, global and per-session.

    Session-local hook arrays shadow the global one, so every session that
    already overrides an event also gets our entry; user entries are kept.
    """
    command = wake_command(channel)
    scopes: list[tuple[str, ...]] = [("-g",)]
    for session in await _sessions():
        scopes.append(("-t", f"{session}:"))
    armed: list[str] = []
    for event in PRESENCE_WAKE_HOOK_EVENTS:
        for scope in scopes:
            if scope != ("-g",) and not await _hook_entries(scope, event):
                continue
            # One unsupported event name must not abort the remaining arms.
            try:
                await _sweep_ours(scope, event, command)
                entries = await _hook_entries(scope, event)
                index = max(entries, default=-1) + 1
                await run("set-hook", *scope, f"{event}[{index}]", command)
            except TmuxError:
                continue
            armed.append(f"{' '.join(scope)}:{event}[{index}]")
    return armed


async def remove_focus_wake_hooks(channel: str = PRESENCE_WAKE_CHANNEL) -> None:
    """Remove only entries whose body is our wake command, in every scope."""
    command = wake_command(channel)
    scopes: list[tuple[str, ...]] = [("-g",)]
    for session in await _sessions():
        scopes.append(("-t", f"{session}:"))
    for event in PRESENCE_WAKE_HOOK_EVENTS:
        for scope in scopes:
            await _sweep_ours(scope, event, command)


async def wait_for_wake(channel: str = PRESENCE_WAKE_CHANNEL) -> None:
    """Block in one tmux client until a hook signals the channel.

    Cancellation kills and reaps the waiter so no orphan client survives.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "wait-for",
        channel,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await proc.wait()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
