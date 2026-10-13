"""The `next` step a refusal hands an agent; one place so wording changes are one-line edits."""

from __future__ import annotations

from typing import Final

NEXT_BUSY: Final = 'send again with if_busy="queue", "amend", or "replace"'
NEXT_SEND_BUSY: Final = (
    'send again without if_busy="refuse": omit if_busy to queue behind the current work'
)
NEXT_QUEUE_FULL: Final = (
    "await or cancel_job one of your queued handles, then send again; "
    "if you have none, retry after the queue drains"
)
NEXT_STEER_STALE: Final = 'send with if_busy="queue", or if_busy="replace"'
NEXT_REPLACE_TIMEOUT: Final = (
    "nothing was sent and the interrupt stays applied; await the interrupted handle, then send"
)
NEXT_NAME_TAKEN: Final = "omit name to get a free one"
NEXT_HUMAN_PRESENT: Final = "ask the user to leave the pane, then retry"
NEXT_AWAITING_DECISION: Final = (
    "ask the user to answer the dialog in the target's terminal, then re-send; "
    "omit if_busy to queue the message until it closes"
)
NEXT_AWAITING_DECISION_SETTINGS: Final = (
    "ask the user to answer the dialog in the target's terminal, then retry the settings update"
)
NEXT_SELF_SEND: Final = (
    "do the work in your current turn and return its answer; you cannot queue to or await yourself"
)
NEXT_NO_TERMINAL: Final = "no terminal exists for this participant; do not retry this control"
NEXT_FOCUS_EVENTS_OFF: Final = (
    "the pane is selected in an attached tmux client and focus events are off: "
    "ask the user to detach or enable tmux focus-events"
)
NEXT_RETRY_ONCE: Final = "retry once"
NEXT_FORGET: Final = "no terminal exists for this participant: kill(forget=true)"
NEXT_ASK_OPERATOR: Final = (
    "no terminal exists for this participant: ask the operator to run `theater kill --forget`"
)
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
