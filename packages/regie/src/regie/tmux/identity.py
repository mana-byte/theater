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
_IDENTITY_OPTION = f"{_MARKER_PREFIX}identity"
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
        f"#{{{_IDENTITY_OPTION}}}",
        "#{pane_current_path}",
        "#{session_name}",
        "#{window_name}",
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
    cwd: str | None = None
    session_name: str | None = None
    window_name: str | None = None

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
    if len(parts) not in {15, 16, 19} or not all(parts[:6]) or not _PANE_ID.fullmatch(parts[3]):
        raise TmuxError("tmux returned an invalid pane identity")
    try:
        pane_pid = int(parts[4])
    except ValueError:
        raise TmuxError("tmux returned an invalid pane process identity") from None
    if pane_pid <= 0 or parts[5] not in {"0", "1"}:
        raise TmuxError("tmux returned an invalid pane process identity")
    server = ServerIdentity(*parts[:3]).value
    provider_id, incarnation, occupant_id, digest, raw_pid, launch_id, launch_executable = (
        value or None for value in parts[8:15]
    )
    if len(parts) in {16, 19} and parts[15]:
        marker = _parse_identity_marker(parts[15])
        if marker is not None:
            (
                provider_id,
                incarnation,
                occupant_id,
                digest,
                occupant_pane_pid,
                launch_id,
                launch_executable,
            ) = marker
        else:
            provider_id = incarnation = occupant_id = digest = launch_id = launch_executable = None
            occupant_pane_pid = None
    else:
        try:
            occupant_pane_pid = None if raw_pid is None else int(raw_pid)
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
        provider_id=provider_id,
        terminal_incarnation=incarnation,
        occupant_id=occupant_id,
        occupant_digest=digest,
        occupant_pane_pid=occupant_pane_pid,
        launch_id=launch_id,
        launch_executable=launch_executable,
        cwd=parts[16] or None if len(parts) == 19 else None,
        session_name=parts[17] or None if len(parts) == 19 else None,
        window_name=parts[18] or None if len(parts) == 19 else None,
    )


async def pane_snapshot(pane_id: str) -> PaneSnapshot | None:
    if not _PANE_ID.fullmatch(pane_id):
        raise TmuxError(f"invalid tmux pane id {pane_id!r}")
    output = await run("display-message", "-p", "-t", pane_id, _FORMAT, check=False)
    if not output:
        return None
    parts = output.split("\t")
    if len(parts) in {15, 16, 19} and not parts[3]:
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
    marker = {
        "provider_id": provider_id,
        "terminal_incarnation": terminal_incarnation,
        "occupant_id": occupant_id,
        "occupant_digest": occupant_digest(occupant_id),
        "pane_pid": pane_pid,
        "launch_id": launch_id,
        "executable": executable,
    }
    for item in marker.values():
        encoded = str(item)
        if any(character in encoded for character in "\r\n\x00"):
            raise TmuxError("terminal identity contains an invalid control character")
    await run(
        "set-option",
        "-p",
        "-t",
        pane_id,
        _IDENTITY_OPTION,
        json.dumps(marker, allow_nan=False, separators=(",", ":"), sort_keys=True),
    )


def _parse_identity_marker(
    raw: str,
) -> tuple[str, str, str, str, int, str, str] | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    strings = (
        "provider_id",
        "terminal_incarnation",
        "occupant_id",
        "occupant_digest",
        "launch_id",
        "executable",
    )
    if any(not isinstance(value.get(name), str) or not value[name] for name in strings):
        return None
    pane_pid = value.get("pane_pid")
    if type(pane_pid) is not int or pane_pid <= 0:
        return None
    return (
        str(value["provider_id"]),
        str(value["terminal_incarnation"]),
        str(value["occupant_id"]),
        str(value["occupant_digest"]),
        pane_pid,
        str(value["launch_id"]),
        str(value["executable"]),
    )


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
