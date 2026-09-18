"""Exact tmux server, pane, and Régie occupant identity."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from regie.tmux.command import TmuxError, run

_PANE_ID = re.compile(r"^%[0-9]+$")
_MAX_IDENTITY_COMPONENT = 256
_MAX_IDENTITY = 512
_MARKER_PREFIX = "@regie-bridge-"
_FORMAT = "\t".join(
    (
        "#{socket_path}",
        "#{pid}",
        "#{start_time}",
        "#{pane_id}",
        "#{pane_pid}",
        "#{pane_dead}",
        "#{pane_current_command}",
        "#{window_id}",
        f"#{{{_MARKER_PREFIX}provider}}",
        f"#{{{_MARKER_PREFIX}incarnation}}",
        f"#{{{_MARKER_PREFIX}occupant}}",
        f"#{{{_MARKER_PREFIX}occupant-digest}}",
        f"#{{{_MARKER_PREFIX}pane-pid}}",
        f"#{{{_MARKER_PREFIX}launch}}",
        f"#{{{_MARKER_PREFIX}executable}}",
    )
)


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    socket_path: str
    pid: str
    started_at: str

    def __post_init__(self) -> None:
        values = (self.socket_path, self.pid, self.started_at)
        if sum(map(len, values)) > _MAX_IDENTITY:
            raise TmuxError("tmux returned an invalid server identity")
        if any(
            not value
            or len(value) > _MAX_IDENTITY_COMPONENT
            or any(character in value for character in "\t\r\n")
            for value in values
        ):
            raise TmuxError("tmux returned an invalid server identity")

    @property
    def value(self) -> str:
        return json.dumps((self.socket_path, self.pid, self.started_at), separators=(",", ":"))

    @classmethod
    def parse(cls, value: str) -> ServerIdentity:
        try:
            parts = json.loads(value)
        except (TypeError, ValueError):
            raise TmuxError("tmux server identity is invalid") from None
        if (
            not isinstance(parts, list)
            or len(parts) != 3
            or not all(isinstance(part, str) for part in parts)
        ):
            raise TmuxError("tmux server identity is invalid")
        return cls(*parts)


@dataclass(frozen=True, slots=True)
class PaneSnapshot:
    server_identity: str
    pane_id: str
    pane_pid: int
    dead: bool
    executable: str
    window_id: str
    provider_id: str | None
    terminal_incarnation: str | None
    occupant_id: str | None
    occupant_digest: str | None
    occupant_pane_pid: int | None
    launch_id: str | None
    launch_executable: str | None

    @property
    def managed(self) -> bool:
        return all(
            (
                self.provider_id,
                self.terminal_incarnation,
                self.occupant_id,
                self.occupant_digest,
                self.occupant_pane_pid,
                self.launch_id,
                self.launch_executable,
            )
        )


def occupant_digest(occupant_id: str) -> str:
    return hashlib.sha256(occupant_id.encode("utf-8")).hexdigest()


def parse_snapshot(line: str) -> PaneSnapshot:
    parts = line.split("\t")
    if len(parts) != 15 or not all(parts[:6]) or not _PANE_ID.fullmatch(parts[3]):
        raise TmuxError("tmux returned an invalid pane identity")
    try:
        pane_pid = int(parts[4])
    except ValueError:
        raise TmuxError("tmux returned an invalid pane process identity") from None
    if pane_pid <= 0 or parts[5] not in {"0", "1"}:
        raise TmuxError("tmux returned an invalid pane process identity")
    server = ServerIdentity(*parts[:3]).value
    optional = tuple(value or None for value in parts[8:])
    try:
        occupant_pane_pid = None if optional[4] is None else int(optional[4])
    except ValueError:
        raise TmuxError("tmux returned invalid occupant process evidence") from None
    if occupant_pane_pid is not None and occupant_pane_pid <= 0:
        raise TmuxError("tmux returned invalid occupant process evidence")
    return PaneSnapshot(
        server_identity=server,
        pane_id=parts[3],
        pane_pid=pane_pid,
        dead=parts[5] == "1",
        executable=parts[6],
        window_id=parts[7],
        provider_id=optional[0],
        terminal_incarnation=optional[1],
        occupant_id=optional[2],
        occupant_digest=optional[3],
        occupant_pane_pid=occupant_pane_pid,
        launch_id=optional[5],
        launch_executable=optional[6],
    )


async def pane_snapshot(pane_id: str) -> PaneSnapshot | None:
    if not _PANE_ID.fullmatch(pane_id):
        raise TmuxError(f"invalid tmux pane id {pane_id!r}")
    output = await run("display-message", "-p", "-t", pane_id, _FORMAT, check=False)
    if not output:
        return None
    parts = output.split("\t")
    if len(parts) == 15 and not parts[3]:
        return None
    return parse_snapshot(output)


async def pane_inventory() -> tuple[PaneSnapshot, ...]:
    output = await run("list-panes", "-a", "-F", _FORMAT)
    snapshots = tuple(parse_snapshot(line) for line in output.splitlines() if line)
    if len({snapshot.server_identity for snapshot in snapshots}) > 1:
        raise TmuxError("tmux returned panes from mixed server identities")
    return snapshots


async def current_server_identity() -> str:
    output = await run("display-message", "-p", "#{socket_path}\t#{pid}\t#{start_time}")
    parts = output.split("\t")
    if len(parts) != 3:
        raise TmuxError("tmux returned an invalid server identity")
    return ServerIdentity(*parts).value


async def mark_pane(
    pane_id: str,
    *,
    provider_id: str,
    terminal_incarnation: str,
    occupant_id: str,
    pane_pid: int,
    launch_id: str,
    executable: str,
) -> None:
    values = {
        "provider": provider_id,
        "incarnation": terminal_incarnation,
        "occupant": occupant_id,
        "occupant-digest": occupant_digest(occupant_id),
        "pane-pid": str(pane_pid),
        "launch": launch_id,
        "executable": executable,
    }
    for name, value in values.items():
        if any(character in value for character in "\r\n\x00"):
            raise TmuxError("terminal identity contains an invalid control character")
        await run("set-option", "-p", "-t", pane_id, f"{_MARKER_PREFIX}{name}", value)


def exact_match(
    snapshot: PaneSnapshot,
    *,
    server_identity: str,
    provider_id: str | None = None,
    terminal_incarnation: str,
    occupant_id: str,
    pane_pid: int | None = None,
) -> bool:
    return (
        not snapshot.dead
        and snapshot.server_identity == server_identity
        and snapshot.terminal_incarnation == terminal_incarnation
        and snapshot.occupant_id == occupant_id
        and snapshot.occupant_digest == occupant_digest(occupant_id)
        and snapshot.occupant_pane_pid == snapshot.pane_pid
        and (provider_id is None or snapshot.provider_id == provider_id)
        and (pane_pid is None or snapshot.pane_pid == pane_pid)
    )


__all__ = [
    "PaneSnapshot",
    "ServerIdentity",
    "current_server_identity",
    "exact_match",
    "mark_pane",
    "occupant_digest",
    "pane_inventory",
    "pane_snapshot",
]
