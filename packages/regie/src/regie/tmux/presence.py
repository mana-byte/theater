"""Fail-closed focus and copy-mode evidence for one tmux server."""

from __future__ import annotations

from dataclasses import dataclass

from regie.tmux.command import TmuxError, run
from regie.tmux.identity import PaneSnapshot, pane_inventory

_CLIENT_FORMAT = "\t".join(
    (
        "#{client_flags}",
        "#{client_readonly}",
        "#{client_control_mode}",
        "#{window_id}",
        "#{pane_id}",
        "#{client_termfeatures}",
    )
)


@dataclass(frozen=True, slots=True)
class PresenceEvidence:
    state: str
    reason: str
    mode: str | None


@dataclass(frozen=True, slots=True)
class _Client:
    flags: frozenset[str]
    readonly: bool
    control: bool
    window_id: str
    pane_id: str
    features: frozenset[str]

    @property
    def input_capable(self) -> bool:
        return not self.readonly and not self.control


def _parse_clients(output: str) -> tuple[_Client, ...]:
    clients: list[_Client] = []
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 6 or parts[1] not in {"0", "1"} or parts[2] not in {"0", "1"}:
            raise TmuxError("tmux returned invalid focus evidence")
        clients.append(
            _Client(
                flags=frozenset(value for value in parts[0].split(",") if value),
                readonly=parts[1] == "1",
                control=parts[2] == "1",
                window_id=parts[3],
                pane_id=parts[4],
                features=frozenset(value for value in parts[5].split(",") if value),
            )
        )
    return tuple(clients)


async def observe_presence(expected: PaneSnapshot) -> PresenceEvidence:
    """Bracket focus evidence with pane identity and fail closed on drift."""
    before = await pane_inventory()
    match = next((pane for pane in before if pane.pane_id == expected.pane_id), None)
    if match != expected:
        return PresenceEvidence("unknown", "terminal_identity_changed", None)
    clients_output = await run("list-clients", "-F", _CLIENT_FORMAT)
    focus_events = await run("show-options", "-g", "-v", "focus-events", check=False)
    mode = await run("display-message", "-p", "-t", expected.pane_id, "#{pane_in_mode}")
    after = await pane_inventory()
    if before != after:
        return PresenceEvidence("unknown", "terminal_topology_changed", None)
    clients = _parse_clients(clients_output)
    relevant = tuple(
        client
        for client in clients
        if client.input_capable and client.window_id == expected.window_id
    )
    pane_mode = "copy" if mode and mode != "0" else None
    if not relevant:
        return PresenceEvidence("absent", "no_input_capable_viewer", pane_mode)
    if focus_events.strip() != "on":
        return PresenceEvidence("unknown", "focus_events_unavailable", pane_mode)
    if any("focus" not in client.features for client in relevant):
        return PresenceEvidence("unknown", "focus_reporting_unavailable", pane_mode)
    if any("focused" in client.flags and client.pane_id == expected.pane_id for client in relevant):
        return PresenceEvidence("present", "focused_viewer", pane_mode)
    if any("active-pane" in client.flags for client in relevant):
        return PresenceEvidence("unknown", "independent_active_pane", pane_mode)
    if any("focused" not in client.flags for client in relevant):
        return PresenceEvidence("unknown", "focus_unverified", pane_mode)
    return PresenceEvidence("absent", "pane_released", pane_mode)


__all__ = ["PresenceEvidence", "observe_presence"]
