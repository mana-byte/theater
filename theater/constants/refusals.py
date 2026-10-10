"""The `next` step a refusal hands an agent; one place so wording changes are one-line edits."""

from __future__ import annotations

from typing import Final

NEXT_BUSY: Final = "queue_followup, or interrupt_session then send once it is idle"
NEXT_STEER_STALE: Final = "interrupt_session, then send once it is idle; or queue_followup"
NEXT_NAME_TAKEN: Final = "omit name to get a free one"
NEXT_HUMAN_PRESENT: Final = "ask the user to leave the pane, then retry"
NEXT_NO_TERMINAL: Final = "no terminal exists for this participant; do not retry this control"
NEXT_FOCUS_EVENTS_OFF: Final = (
    "the pane is selected in an attached tmux client and focus events are off: "
    "ask the user to detach or enable tmux focus-events"
)
NEXT_RETRY_ONCE: Final = "retry once"
NEXT_NOT_YOUR_CHILD: Final = (
    "ask its control owner {owner!r} to act, or have the operator run `theater control-transfer`"
)
NEXT_DAEMON_UNAVAILABLE: Final = "list_participants, or await the handle"
#: Presence reason meaning no terminal binding exists, so waiting for a human cannot help.
NO_TERMINAL_PRESENCE_REASON: Final = "no-terminal-presence-evidence"
#: tmux cannot report focus for a selected pane: a human may be there, so never wait it out.
FOCUS_EVENTS_UNAVAILABLE_REASON: Final = "focus_events_unavailable"
#: Provider-state churn that a fresh inspection settles within seconds.
TRANSIENT_PRESENCE_REASONS: Final = frozenset(
    {
        "provider-generation-stale",
        "provider-presence-invalidated",
        "provider-presence-changed-during-inspect",
    }
)


def human_present_next(reason: str) -> str:
    """Pick the human_present next step from the presence snapshot reason."""
    if reason == NO_TERMINAL_PRESENCE_REASON:
        return NEXT_NO_TERMINAL
    if reason == FOCUS_EVENTS_UNAVAILABLE_REASON:
        return NEXT_FOCUS_EVENTS_OFF
    if reason in TRANSIENT_PRESENCE_REASONS:
        return NEXT_RETRY_ONCE
    return NEXT_HUMAN_PRESENT
