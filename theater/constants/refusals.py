"""The `next` step a refusal hands an agent; one place so wording changes are one-line edits."""

from __future__ import annotations

from typing import Final

NEXT_BUSY: Final = "queue_followup, or interrupt_session then send once it is idle"
NEXT_STEER_STALE: Final = "interrupt_session, then send once it is idle; or queue_followup"
NEXT_NAME_TAKEN: Final = "omit name to get a free one"
NEXT_HUMAN_PRESENT: Final = "ask the user to leave the pane, then retry"
NEXT_NO_TERMINAL: Final = "no terminal exists for this participant; do not retry this control"
NEXT_NOT_YOUR_CHILD: Final = (
    "ask its control owner {owner!r} to act, or have the operator run `theater control-transfer`"
)
NEXT_DAEMON_UNAVAILABLE: Final = "list_participants, or await the handle"
#: Presence reason meaning no terminal binding exists, so waiting for a human cannot help.
NO_TERMINAL_PRESENCE_REASON: Final = "no-terminal-presence-evidence"


def human_present_next(reason: str) -> str:
    """Pick the human_present next step from the presence snapshot reason."""
    return NEXT_NO_TERMINAL if reason == NO_TERMINAL_PRESENCE_REASON else NEXT_HUMAN_PRESENT
