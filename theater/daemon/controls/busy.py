"""The typed vocabulary of one busy refusal: action, ordering, wording.

``_reject_busy`` walks the facts in :class:`BusyAction` declaration order and
raises the selected refusal, worded for the operation that was refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from theater.models import Busy

__all__ = ["BusyAction", "BusyOperation", "BusyRefusal", "busy_refusal"]


class BusyAction(Enum):
    """Refusal causes in the order ``_reject_busy`` walks them.

    Liveliness outranks the queue (nothing drains without a live runtime);
    the queue outranks an active turn only because it drains when the turn ends.
    """

    RESTORE_RUNTIME = "restore_runtime"
    RESTORE_IDENTITY = "restore_identity"
    RESOLVE_UNKNOWN_STATE = "resolve_unknown_state"
    AWAIT_QUEUE = "await_queue"
    AWAIT_TURN_END = "await_turn_end"
    AWAIT_BARRIER = "await_barrier"
    AWAIT_JOBS = "await_jobs"


class BusyOperation(Enum):
    """The refused control a refusal is worded for."""

    SEND = "send"
    SETTINGS = "settings"


@dataclass(frozen=True, slots=True)
class BusyRefusal:
    """The selected action plus the facts its wording needs."""

    action: BusyAction
    queued: int = 0
    running_handle: str | None = None


def busy_refusal(
    participant_id: str,
    refusal: BusyRefusal,
    *,
    turn: str | None,
    operation: BusyOperation,
) -> Busy:
    """Compose the Busy error from the selected action's operation-aware wording."""
    action = refusal.action
    sending = operation is BusyOperation.SEND
    if action is BusyAction.RESTORE_RUNTIME:
        doing = "not injecting a new prompt" if sending else "not delivering a settings update"
        return Busy(
            f"participant {participant_id!r} has no live native connection whose "
            f"state can prove idle; {doing}"
        )
    if action is BusyAction.RESTORE_IDENTITY:
        return Busy(
            f"participant {participant_id!r} has no exact native session identity; "
            "not treating that missing identity as idle"
        )
    if action is BusyAction.RESOLVE_UNKNOWN_STATE:
        doing = "no prompt is injected" if sending else "no settings update is delivered"
        return Busy(
            f"participant {participant_id!r} has unknown native execution state; "
            f"UNKNOWN is not proof of idle, so {doing}"
        )
    if action is BusyAction.AWAIT_QUEUE:
        if sending:
            return Busy(
                f"participant {participant_id!r} has {refusal.queued} queued followup(s); "
                "an ordinary send cannot jump ahead of them — await the queued "
                "handles or queue another followup instead"
            )
        return Busy(
            f"participant {participant_id!r} has {refusal.queued} queued followup(s); "
            "a settings update cannot jump ahead of them — wait for the queue "
            "to drain, then retry once idle"
        )
    if action is BusyAction.AWAIT_TURN_END:
        described = (
            f" native turn {turn!r}"
            if turn is not None
            else " active native execution without a reported turn id"
        )
        if sending:
            return Busy(
                f"participant {participant_id!r} has{described}; not injecting a new prompt"
                ". Call interrupt, wait for idle, or queue a followup"
            )
        return Busy(
            f"participant {participant_id!r} has{described}; not delivering a settings "
            "update — settings require an idle participant, so wait for the "
            "turn to end, then retry"
        )
    if action is BusyAction.AWAIT_BARRIER:
        waiting = "no subsequent prompt is delivered" if sending else "a settings update waits too"
        return Busy(
            f"participant {participant_id!r} has an unresolved native prompt delivery; "
            f"{waiting} until exact terminal evidence or an authoritative idle "
            "state clears its generation/session-bound barrier"
        )
    assert action is BusyAction.AWAIT_JOBS
    if sending:
        return Busy(
            f"participant {participant_id!r} has a running send job "
            f"({refusal.running_handle}); not injecting a new prompt"
        )
    return Busy(
        f"participant {participant_id!r} has a running send job "
        f"({refusal.running_handle}); not delivering a settings update — wait "
        "for it to finish, then retry"
    )
