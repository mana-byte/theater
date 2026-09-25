"""Transitions that deserve attention when a fresh state projection arrives."""

from __future__ import annotations

from theater.frontend import StateProjection


def newly_awaiting_input(
    previous: StateProjection | None, current: StateProjection
) -> tuple[str, ...]:
    """Include initial input requests; repeated projections and stale state stay quiet."""
    if current.stale:
        return ()
    return tuple(
        participant_id
        for participant_id, participant in current.participants.items()
        if participant.status == "awaiting_input"
        and (
            previous is None
            or (before := previous.participants.get(participant_id)) is None
            or before.status != "awaiting_input"
        )
    )
