"""Bounded epoch-scoped telemetry emission state."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from theater.constants.observability import (
    AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT,
    AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT,
)


@dataclass(slots=True)
class ParticipantEmissionState:
    """The recent emitted signals for one participant source epoch."""

    source_epoch: str
    emitted: OrderedDict[tuple[str, str], None] = field(default_factory=OrderedDict)


class AgentTelemetryState:
    """Bounds participant state and retains no cross-epoch signal keys."""

    def __init__(self) -> None:
        self._participants: OrderedDict[str, ParticipantEmissionState] = OrderedDict()

    def for_participant(self, participant_id: str, source_epoch: str) -> ParticipantEmissionState:
        """Return fresh state when a participant's source epoch changes."""
        state = self._participants.get(participant_id)
        if state is None or state.source_epoch != source_epoch:
            state = ParticipantEmissionState(source_epoch)
            self._participants[participant_id] = state
        self._participants.move_to_end(participant_id)
        while len(self._participants) > AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT:
            self._participants.popitem(last=False)
        return state

    def discard(self, participant_id: str) -> None:
        """Forget one participant's emission state."""
        self._participants.pop(participant_id, None)

    @staticmethod
    def contains(state: ParticipantEmissionState, metric_name: str, signal_id: str) -> bool:
        """Whether this metric signal was already emitted in the source epoch."""
        return (metric_name, signal_id) in state.emitted

    @staticmethod
    def remember(state: ParticipantEmissionState, metric_name: str, signal_id: str) -> None:
        """Remember one successfully emitted signal within its bounded history."""
        state.emitted[(metric_name, signal_id)] = None
        while len(state.emitted) > AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT:
            state.emitted.popitem(last=False)


__all__ = ["AgentTelemetryState", "ParticipantEmissionState"]
