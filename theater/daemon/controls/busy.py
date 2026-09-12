"""The typed vocabulary of one busy refusal: action, ordering, wording.

``_reject_busy`` walks the facts in :class:`BusyAction` order and raises the
first applicable refusal, so the remedy the message names is always the one
the ordering chose — the coupling is the fix for the queue-first order that
told queued-and-disconnected callers to await an undrainable queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from theater.models import Busy

__all__ = ["BusyAction", "BusyRefusal", "busy_refusal"]


class BusyAction(Enum):
    """Why a control operation was refused, in the order ``_reject_busy`` checks.

    Runtime liveliness outranks the queue: followups cannot drain without a
    live runtime, so liveliness refusals are named before the queue's. The
    queue outranks an active turn only because the queue drains when that
    turn ends.
    """

    RESTORE_RUNTIME = "restore_runtime"
    RESTORE_IDENTITY = "restore_identity"
    RESOLVE_UNKNOWN_STATE = "resolve_unknown_state"
    AWAIT_QUEUE = "await_queue"
    AWAIT_TURN_END = "await_turn_end"
    AWAIT_BARRIER = "await_barrier"
    AWAIT_JOBS = "await_jobs"


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
    idle_only: bool = False,
) -> Busy:
    """Compose the Busy error from the selected action's wording."""
    action = refusal.action
    if action is BusyAction.RESTORE_RUNTIME:
        return Busy(
            f"participant {participant_id!r} has no live native connection whose "
            "state can prove idle; not injecting a new prompt"
        )
    if action is BusyAction.RESTORE_IDENTITY:
        return Busy(
            f"participant {participant_id!r} has no exact native session identity; "
            "not treating that missing identity as idle"
        )
    if action is BusyAction.RESOLVE_UNKNOWN_STATE:
        return Busy(
            f"participant {participant_id!r} has unknown native execution state; "
            "UNKNOWN is not proof of idle, so no prompt is injected"
        )
    if action is BusyAction.AWAIT_QUEUE:
        return Busy(
            f"participant {participant_id!r} has {refusal.queued} queued followup(s); "
            "an ordinary send cannot jump ahead of them — await the queued "
            "handles or queue another followup instead"
        )
    if action is BusyAction.AWAIT_TURN_END:
        described = (
            f" native turn {turn!r}"
            if turn is not None
            else " active native execution without a reported turn id"
        )
        return Busy(
            f"participant {participant_id!r} has{described}; not injecting a new prompt"
            + ("" if idle_only else ". Call interrupt, wait for idle, or queue a followup")
        )
    if action is BusyAction.AWAIT_BARRIER:
        return Busy(
            f"participant {participant_id!r} has an unresolved native prompt delivery; "
            "no subsequent prompt is delivered until exact terminal evidence or an "
            "authoritative idle state clears its generation/session-bound barrier"
        )
    assert action is BusyAction.AWAIT_JOBS
    return Busy(
        f"participant {participant_id!r} has a running send job "
        f"({refusal.running_handle}); not injecting a new prompt"
    )
