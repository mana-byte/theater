"""Human-presence facts for one tmux server: inventory, hooks, waiter."""

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
_IDENTITY = "#{socket_path}\t#{pid}\t#{start_time}"
#: The pane's process binds the pane id to one launch epoch.
_FOCUS_PANE_FORMAT = f"#{{pane_id}}{_SEP}#{{window_id}}{_SEP}#{{pane_pid}}{_SEP}{_IDENTITY}"
_FOCUS_CLIENT_FORMAT = _SEP.join(
    (
        "#{client_tty}",
        "#{client_pid}",
        "#{client_created}",
        "#{client_session}",
        "#{session_id}",
        "#{session_created}",
        "#{client_flags}",
        "#{client_readonly}",
        "#{client_control_mode}",
        "#{window_id}",
        "#{pane_id}",
        "#{client_termfeatures}",
    )
)


# Proxy: delegate to client.run at call time so both seam styles patch one place.
async def run(*args: str, check: bool = True) -> str:
    from theater.tmux.client import run as _run

    return await _run(*args, check=check)


async def human_present(pane_id: str) -> bool:
    """Copy-mode check; query errors propagate, never read as "no human"."""
    in_mode = await run("display-message", "-p", "-t", pane_id, "#{pane_in_mode}")
    return bool(in_mode and in_mode != "0")


# ---- focus inventory ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class FocusClient:
    """One attached client; identity spans tty and its session's lifetime."""

    tty: str
    pid: str
    created: str
    session: str
    session_id: str
    session_created: str
    flags: frozenset[str]
    readonly: bool
    control: bool
    window_id: str
    active_pane_id: str
    termfeatures: frozenset[str]

    @property
    def identity(self) -> tuple[str, ...]:
        return (self.tty, self.pid, self.created, self.session_id, self.session_created)

    @property
    def focused(self) -> bool:
        return PRESENCE_FLAG_FOCUSED in self.flags

    @property
    def input_capable(self) -> bool:
        return not self.readonly and not self.control

    @property
    def focus_reporting(self) -> bool:
        return PRESENCE_FEATURE_FOCUS in self.termfeatures


@dataclass(frozen=True, slots=True)
class FocusInventory:
    """One server epoch's panes, pane pids, clients, and server identity."""

    server_identity: str
    panes: dict[str, str]
    pane_pids: dict[str, str]
    clients: tuple[FocusClient, ...]
    observed_at: float
    focus_events_enabled: bool = True


def parse_focus_inventory(
    pane_out: str,
    client_out: str,
    *,
    observed_at: float,
    focus_events_enabled: bool = True,
) -> FocusInventory:
    """Strict parse of `list-panes`/`list-clients`; malformed rows raise."""
    identities: set[str] = set()
    panes: dict[str, str] = {}
    pane_pids: dict[str, str] = {}
    for line in pane_out.splitlines():
        if not line:
            continue
        parts = line.split(_SEP)
        if len(parts) != 4:
            raise TmuxError(f"unexpected focus pane row: {line!r}")
        pane_id, window_id, pane_pid, identity = parts
        if not pane_id or not window_id or not pane_pid or "\t" not in identity:
            raise TmuxError(f"unexpected focus pane row: {line!r}")
        identities.add(TmuxServerIdentity(*identity.split("\t")).value)
        panes[pane_id] = window_id
        pane_pids[pane_id] = pane_pid
    if len(identities) > 1:
        raise TmuxError("tmux returned a mixed-server focus inventory")
    if not identities:
        return FocusInventory("", {}, {}, (), observed_at, focus_events_enabled)
    clients: list[FocusClient] = []
    for line in client_out.splitlines():
        if not line:
            continue
        parts = line.split(_SEP)
        if len(parts) != 12 or parts[7] not in {"0", "1"} or parts[8] not in {"0", "1"}:
            raise TmuxError(f"unexpected focus client row: {line!r}")
        if parts[7:9] == ["0", "0"] and (not all(parts[:7]) or not parts[9]):
            raise TmuxError(f"unidentified input-capable focus client: {line!r}")
        clients.append(
            FocusClient(
                tty=parts[0],
                pid=parts[1],
                created=parts[2],
                session=parts[3],
                session_id=parts[4],
                session_created=parts[5],
                flags=frozenset(f for f in parts[6].split(",") if f),
                readonly=parts[7] == "1",
                control=parts[8] == "1",
                window_id=parts[9],
                active_pane_id=parts[10],
                termfeatures=frozenset(f for f in parts[11].split(",") if f),
            )
        )
    return FocusInventory(
        identities.pop(), panes, pane_pids, tuple(clients), observed_at, focus_events_enabled
    )


async def observe_focus_inventory(*, clock=None) -> FocusInventory:
    """Panes and clients of one verified server epoch, bracket-checked."""
    import time

    observed_at = clock() if clock is not None else time.time()
    pane_out = await run("list-panes", "-a", "-F", _FOCUS_PANE_FORMAT, check=True)
    # No attached clients is an empty rc-0 listing, not an error.
    client_out = await run("list-clients", "-F", _FOCUS_CLIENT_FORMAT, check=True)
    # The option rides every observation: admission never trusts a stale arm.
    option_out = await run("show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION, check=False)
    # A restart between queries would mix old panes with new clients, so the
    # epoch is re-read after: any drift fails the whole observation closed.
    after_out = await run("list-panes", "-a", "-F", _FOCUS_PANE_FORMAT, check=True)
    inventory = parse_focus_inventory(
        pane_out,
        client_out,
        observed_at=observed_at,
        focus_events_enabled=option_out.strip() == "on",
    )
    after = parse_focus_inventory(after_out, "", observed_at=observed_at)
    if (
        after.server_identity != inventory.server_identity
        or after.panes != inventory.panes
        or after.pane_pids != inventory.pane_pids
    ):
        raise TmuxError("tmux server or pane topology changed during observation")
    return inventory


# ---- focus events ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FocusEventsStatus:
    """Result of ensuring the focus-events option; drives reattach advice."""

    enabled: bool
    previously_off: bool
    focusless_clients: tuple[str, ...]


async def ensure_focus_events() -> FocusEventsStatus:
    """Turn focus-events on globally; never detach clients to pick it up."""
    current = await run("show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION, check=False)
    previously_off = current.strip() != "on"
    if previously_off:
        await run("set-option", "-g", PRESENCE_FOCUS_EVENTS_OPTION, "on")
    # Verify, never assume: an unset option must not read as armed.
    verified = (
        await run("show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION, check=False)
    ).strip() == "on"
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
    return FocusEventsStatus(verified, previously_off, tuple(focusless))


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
    """Append owned wake entries per event, global and shadowing sessions."""
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
    """Block one tmux client until a hook signals; nonzero exits are errors."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "wait-for",
        channel,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        code = await proc.wait()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    if code != 0:
        raise TmuxError(f"tmux wait-for {channel} exited with {code}")
