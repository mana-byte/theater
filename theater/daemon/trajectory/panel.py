"""Pure trajectory panel-state decisions."""

from __future__ import annotations

from theater.daemon.trajectory.history import HistoryLoad
from theater.models import Participant, Status, Tier
from theater.provenance import is_trusted_provenance
from theater.trajectory import PanelState, PanelStateInfo, TrajectoryParticipantState
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE


def participant_state(participant: Participant) -> TrajectoryParticipantState:
    if participant.status is Status.DEAD:
        return TrajectoryParticipantState.DEAD
    if participant.tier is Tier.EXTERNAL:
        return TrajectoryParticipantState.EXTERNAL
    return TrajectoryParticipantState.LIVE


def initial_panel_state(
    participant: Participant,
    *,
    current_participant_state: TrajectoryParticipantState,
    result: HistoryLoad,
    has_transcript: bool,
    live_allowed: bool,
) -> PanelStateInfo:
    page = result.page
    if result.trusted and (
        page.location is not None
        or page.events
        or page.trajectory
        or has_transcript
        or (live_allowed and has_transcript)
    ):
        state = PanelState.READY
        message = result.message
    elif result.trusted:
        state = PanelState.WAITING
        message = result.message or "the transcript source is waiting for its first record"
    elif page.error_code == TRANSCRIPT_IDENTITY_LOST_CODE or result.ambiguous:
        state = (
            PanelState.UNAVAILABLE if participant.status is Status.DEAD else PanelState.UNTRUSTED
        )
        message = result.message
    elif page.error_code is None and not is_trusted_provenance(page.provenance):
        state = PanelState.UNTRUSTED
        message = result.message or "transcript identity is not trusted"
    elif has_transcript:
        state = PanelState.STALE
        message = f"transcript history is unavailable; cached records remain ({result.message})"
    else:
        state = PanelState.UNAVAILABLE
        message = result.message or "trajectory history is unavailable"
    return PanelStateInfo(state, message, current_participant_state)


__all__ = ["initial_panel_state", "participant_state"]
