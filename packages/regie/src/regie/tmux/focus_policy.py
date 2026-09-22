"""Pure presence classification and client-lifetime evidence for trusted blur."""

from __future__ import annotations

from dataclasses import dataclass

from regie.tmux.focus_facts import FocusClient, FocusInventory
from regie.tmux.identity import PaneSnapshot


@dataclass(frozen=True, slots=True)
class PresenceEvidence:
    state: str
    reason: str
    mode: str | None
    epoch: int | None = None


class FocusTrust:
    def __init__(self) -> None:
        self.armed = False
        self._clients: dict[tuple[str, ...], tuple[bool, bool]] = {}

    def invalidate(self) -> None:
        self._clients.clear()

    def observe(self, clients: tuple[FocusClient, ...]) -> None:
        previous, self._clients = self._clients, {}
        for client in clients:
            if not self.armed or not client.input_capable or "focus" not in client.features:
                continue
            prior = previous.get(client.identity)
            trusted = prior is not None and (prior[1] or prior[0] != client.focused)
            self._clients[client.identity] = client.focused, trusted

    def blurred(self, client: FocusClient) -> bool:
        evidence = self._clients.get(client.identity)
        return bool(self.armed and not client.focused and evidence and evidence[1])


def classify(expected: PaneSnapshot, facts: FocusInventory, trust: FocusTrust) -> PresenceEvidence:
    pane = facts.panes.get(expected.pane_id)
    if (
        facts.server_identity != expected.server_identity
        or pane is None
        or pane.pid != expected.pane_pid
        or pane.window_id != expected.window_id
    ):
        return PresenceEvidence("unknown", "terminal_identity_changed", None)
    verdicts = {
        _verdict(client, expected.pane_id, facts, trust)
        for client in facts.clients
        if client.input_capable and client.window_id == pane.window_id
    }
    for verdict, state in (
        ("focused_viewer", "present"),
        ("independent_active_pane", "unknown"),
        ("selection_unobservable", "unknown"),
        ("focus_events_unavailable", "unknown"),
        ("focus_unverified", "unknown"),
        ("pane_released", "absent"),
    ):
        if verdict in verdicts:
            return PresenceEvidence(state, verdict, pane.mode)
    return PresenceEvidence("absent", "no_input_capable_viewer", pane.mode)


def _verdict(client: FocusClient, pane_id: str, facts: FocusInventory, trust: FocusTrust) -> str:
    if facts.enabled and trust.blurred(client):
        return "pane_released"
    if "active-pane" in client.flags:
        return "independent_active_pane"
    selected = facts.panes.get(client.pane_id)
    if selected is None or selected.window_id != client.window_id:
        return "selection_unobservable"
    if client.pane_id != pane_id:
        return "pane_released"
    if client.focused:
        return "focused_viewer"
    return "focus_unverified" if facts.enabled else "focus_events_unavailable"
