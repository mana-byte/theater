"""Identity-bearing, bracket-checked focus facts from one pinned tmux server."""

from __future__ import annotations

from dataclasses import dataclass

from regie.tmux.command import TmuxError, run, sequence_argv
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


def _window_views(
    facts: FocusInventory, blurred: frozenset[tuple[str, ...]]
) -> dict[str, frozenset[tuple[FocusClient, str | None, bool]]]:
    """Per window: every input-capable client plus what classification reads about it."""
    views: dict[str, set[tuple[FocusClient, str | None, bool]]] = {}
    for client in facts.clients:
        if not client.input_capable:
            continue
        selected = facts.panes.get(client.pane_id)
        entry = (client, selected.window_id if selected else None, client.identity in blurred)
        views.setdefault(client.window_id, set()).add(entry)
    return {window: frozenset(entries) for window, entries in views.items()}


def changed_panes(
    old: FocusInventory,
    old_blurred: frozenset[tuple[str, ...]],
    new: FocusInventory,
    new_blurred: frozenset[tuple[str, ...]],
) -> frozenset[str] | None:
    """Panes whose presence inputs differ; None when server or focus reporting changed."""
    if old.server_identity != new.server_identity or old.enabled != new.enabled:
        return None
    old_views, new_views = _window_views(old, old_blurred), _window_views(new, new_blurred)
    empty: frozenset[tuple[FocusClient, str | None, bool]] = frozenset()
    changed = set()
    for pane_id in old.panes.keys() | new.panes.keys():
        before, after = old.panes.get(pane_id), new.panes.get(pane_id)
        if before is None or after is None or before != after:
            changed.add(pane_id)
            continue
        if old_views.get(before.window_id, empty) != new_views.get(after.window_id, empty):
            changed.add(pane_id)
    return frozenset(changed)


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


_SECTION = "@regie-focus-section@"


async def read_inventory(server_identity: str) -> FocusInventory:
    """Read panes, clients and the focus option in one tmux process, bracketed by two pane reads."""
    socket = ServerIdentity.parse(server_identity).socket_path
    output = await run(
        "-S",
        socket,
        *sequence_argv(
            (
                ("list-panes", "-a", "-F", _PANES),
                ("display-message", "-p", _SECTION),
                ("list-clients", "-F", _CLIENTS),
                ("display-message", "-p", _SECTION),
                ("show-options", "-g", "-v", "focus-events"),
                ("display-message", "-p", _SECTION),
                ("list-panes", "-a", "-F", _PANES),
            )
        ),
    )
    sections: list[list[str]] = [[]]
    for line in output.split("\n"):
        if line == _SECTION:
            sections.append([])
        else:
            sections[-1].append(line)
    if len(sections) != 4:
        raise TmuxError("tmux returned an invalid focus inventory")
    before_text, clients_text, enabled, after_text = ("\n".join(lines) for lines in sections)
    before = _panes(before_text, server_identity)
    clients = parse_clients(clients_text)
    after = _panes(after_text, server_identity)
    if before != after:
        raise TmuxError("tmux focus topology changed during observation")
    return FocusInventory(server_identity, before, clients, enabled.strip() == "on")
