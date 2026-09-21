"""Identity-bearing, bracket-checked focus facts from one pinned tmux server."""

from __future__ import annotations

from dataclasses import dataclass

from regie.tmux.command import TmuxError, run
from regie.tmux.identity import ServerIdentity

_PANES = "\t".join(
    f"#{{{key}}}"
    for key in (
        "socket_path",
        "pid",
        "start_time",
        "pane_id",
        "window_id",
        "pane_pid",
        "pane_in_mode",
    )
)
_CLIENTS = "\t".join(
    f"#{{{key}}}"
    for key in (
        "client_tty",
        "client_pid",
        "client_created",
        "session_id",
        "session_created",
        "client_flags",
        "client_readonly",
        "client_control_mode",
        "window_id",
        "pane_id",
        "client_termfeatures",
    )
)


@dataclass(frozen=True, slots=True)
class FocusClient:
    identity: tuple[str, ...]
    flags: frozenset[str]
    readonly: bool
    control: bool
    window_id: str
    pane_id: str
    features: frozenset[str]

    @property
    def input_capable(self) -> bool:
        return not self.readonly and not self.control

    @property
    def focused(self) -> bool:
        return "focused" in self.flags


@dataclass(frozen=True, slots=True)
class FocusPane:
    window_id: str
    pid: int
    mode: str | None


@dataclass(frozen=True, slots=True)
class FocusInventory:
    server_identity: str
    panes: dict[str, FocusPane]
    clients: tuple[FocusClient, ...]
    enabled: bool


def parse_clients(output: str) -> tuple[FocusClient, ...]:
    clients = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 11 or parts[6] not in {"0", "1"} or parts[7] not in {"0", "1"}:
            raise TmuxError("tmux returned invalid focus client evidence")
        if parts[6:8] == ["0", "0"] and (not all(parts[:5]) or not parts[8]):
            raise TmuxError("tmux did not identify an input-capable client and its window")
        clients.append(
            FocusClient(
                tuple(parts[:5]),
                frozenset(filter(None, parts[5].split(","))),
                parts[6] == "1",
                parts[7] == "1",
                parts[8],
                parts[9],
                frozenset(filter(None, parts[10].split(","))),
            )
        )
    return tuple(clients)


def _panes(output: str, server_identity: str) -> dict[str, FocusPane]:
    panes = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 7 or not all(parts) or not parts[5].isdigit():
            raise TmuxError("tmux returned invalid focus pane evidence")
        if ServerIdentity(*parts[:3]).value != server_identity or parts[3] in panes:
            raise TmuxError("tmux focus inventory crossed a server or pane identity")
        panes[parts[3]] = FocusPane(parts[4], int(parts[5]), "copy" if parts[6] != "0" else None)
    return panes


async def read_inventory(server_identity: str) -> FocusInventory:
    socket = ServerIdentity.parse(server_identity).socket_path
    before = _panes(await run("-S", socket, "list-panes", "-a", "-F", _PANES), server_identity)
    clients = parse_clients(await run("-S", socket, "list-clients", "-F", _CLIENTS))
    enabled = await run("-S", socket, "show-options", "-g", "-v", "focus-events")
    after = _panes(await run("-S", socket, "list-panes", "-a", "-F", _PANES), server_identity)
    if before != after:
        raise TmuxError("tmux focus topology changed during observation")
    return FocusInventory(server_identity, before, clients, enabled.strip() == "on")
