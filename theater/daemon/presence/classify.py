"""Pure presence classification: focus-literal trust and viewer verdicts."""

from __future__ import annotations

from theater.constants.presence import PRESENCE_FLAG_ACTIVE_PANE
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.models import Status


class FocusTrust:
    """Per-client blur trust; any invalidation resets the epoch."""

    def __init__(self) -> None:
        self._epoch = 0
        self.arm_ok = False
        self._evidence: dict[tuple[str, ...], tuple[bool, int]] = {}

    @property
    def epoch(self) -> int:
        return self._epoch

    def invalidate(self) -> None:
        """Drop every blur fact; a reused lifetime must re-prove its flip."""
        self._epoch += 1
        self._evidence.clear()

    def observe(self, clients) -> None:
        """Record focus literals; trust needs a same-epoch transition."""
        seen: set[tuple[str, ...]] = set()
        for client in clients:
            key = client.identity
            seen.add(key)
            if not client.input_capable or not client.focus_reporting:
                self._evidence.pop(key, None)
                continue
            previous = self._evidence.get(key)
            if previous is None:
                # CLIENT_FOCUSED defaults on: a first sighting proves nothing.
                self._evidence[key] = (client.focused, -1)
                continue
            prev_focused, trusted_epoch = previous
            if client.focused != prev_focused:
                trusted_epoch = self._epoch
            self._evidence[key] = (client.focused, trusted_epoch)
        for gone in set(self._evidence) - seen:
            del self._evidence[gone]

    def is_trusted_blur(self, client) -> bool:
        """Release needs arm validity, focus reporting, and a same-epoch flip."""
        if not self.arm_ok or not client.focus_reporting:
            return False
        evidence = self._evidence.get(client.identity)
        return evidence is not None and evidence[1] == self._epoch


def client_verdict(
    client, pane_id: str, window_id: str, inventory, trust: FocusTrust
) -> str | None:
    """One client's contribution; None when the client cannot matter."""
    if not client.input_capable or client.window_id != window_id:
        return None
    if not client.focused and trust.is_trusted_blur(client):
        # A proven blur releases the pane and the window alike.
        return "released"
    if PRESENCE_FLAG_ACTIVE_PANE in client.flags:
        # The shared active pane is not this client's input pane.
        return "window-viewer"
    if client.active_pane_id == pane_id:
        return "present" if client.focused else "unknown-blur"
    if inventory.panes.get(client.active_pane_id) == window_id:
        # A regular pane selection inside the same window releases ours.
        return "released"
    # Selection unobservable: protect the client's possible scope.
    return "unknown-selection"


def derive(participant, inventory, revision: int, trust: FocusTrust) -> PresenceSnapshot:
    """Fold one fresh inventory into one participant's presence facts."""
    guard = binding_guard(participant, inventory, revision)
    if guard is not None:
        return guard
    window_id = inventory.panes[participant.tmux_pane]
    present = window_viewer = unknown_blur = unknown_selection = released = False
    for client in inventory.clients:
        verdict = client_verdict(client, participant.tmux_pane, window_id, inventory, trust)
        if verdict == "present":
            present = True
        elif verdict == "window-viewer":
            window_viewer = True
        elif verdict == "unknown-blur":
            unknown_blur = True
        elif verdict == "unknown-selection":
            unknown_selection = True
        elif verdict == "released":
            released = True
    observed_at = inventory.observed_at
    if present:
        return PresenceSnapshot(PresenceState.PRESENT, "focused-viewer", revision, observed_at)
    if window_viewer:
        return PresenceSnapshot(
            PresenceState.UNKNOWN, "independent-active-pane", revision, observed_at
        )
    if unknown_blur:
        return PresenceSnapshot(PresenceState.UNKNOWN, "focus-unverified", revision, observed_at)
    if unknown_selection:
        return PresenceSnapshot(
            PresenceState.UNKNOWN, "selection-unobservable", revision, observed_at
        )
    if released:
        return PresenceSnapshot(PresenceState.ABSENT, "pane-released", revision, observed_at)
    return PresenceSnapshot(PresenceState.ABSENT, "no-viewer", revision, observed_at)


def binding_guard(participant, inventory, revision: int) -> PresenceSnapshot | None:
    """UNKNOWN guards that precede any viewer classification."""
    observed_at = inventory.observed_at
    if not participant.tmux_pane:
        return PresenceSnapshot(PresenceState.ABSENT, "no-pane", revision, observed_at)
    expected = participant.tmux_server_identity
    if participant.status is Status.DEAD and (
        (bool(expected) and expected != inventory.server_identity)
        or participant.tmux_pane not in inventory.panes
        or (
            participant.pid is not None
            and inventory.pane_pids.get(participant.tmux_pane) != str(participant.pid)
        )
    ):
        return PresenceSnapshot(PresenceState.ABSENT, "former-pane-gone", revision, observed_at)
    if not expected:
        return PresenceSnapshot(PresenceState.UNKNOWN, "identity-unstamped", revision, observed_at)
    if expected != inventory.server_identity:
        return PresenceSnapshot(
            PresenceState.UNKNOWN, "server-identity-changed", revision, observed_at
        )
    if participant.tmux_pane not in inventory.panes:
        return PresenceSnapshot(
            PresenceState.UNKNOWN, "pane-not-in-inventory", revision, observed_at
        )
    if participant.pid is not None and inventory.pane_pids.get(participant.tmux_pane) != str(
        participant.pid
    ):
        return PresenceSnapshot(PresenceState.UNKNOWN, "pane-pid-changed", revision, observed_at)
    return None
