"""Bounded metadata-only state for epoch-scoped trajectory telemetry."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from theater.constants.observability import (
    AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT,
    AGENT_TELEMETRY_LOG_REVISION_LIMIT,
    AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT,
    AGENT_TELEMETRY_RECORD_SNAPSHOT_LIMIT,
    AGENT_TELEMETRY_SPAN_CONTEXT_LIMIT,
    AGENT_TELEMETRY_UNKNOWN_LABEL,
)
from theater.trajectory import (
    DetailField,
    Timing,
    TrajectoryFailure,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUsage,
)

from .labels import normalize_label


@dataclass(frozen=True, slots=True)
class CanonicalRecordSnapshot:
    """Content-free fields needed to derive request and tool operations later."""

    record_id: str
    revision: int
    participant_id: str
    source_epoch: str
    lane: TrajectoryLane
    kind: TrajectoryKind
    source: str
    status: TrajectoryStatus
    raw_index: int
    source_offset: int | None
    event_ordinal: int
    turn_id: str | None
    step_id: str | None
    request_id: str | None
    call_id: str | None
    parent_call_id: str | None
    mcp_server: str | None
    mcp_tool: str | None
    tool_name: str | None
    timing: Timing | None
    usage: TrajectoryUsage | None
    failure_category: str | None
    failure_code: str | None
    retry_of_record_id: str | None
    retry_attempt: int | None

    @classmethod
    def from_record(cls, record: TrajectoryRecord) -> CanonicalRecordSnapshot:
        """Strip canonical content while preserving scalar operation metadata."""
        tool_name = next(
            (
                detail.preview.text
                for detail in reversed(record.details)
                if detail.name == "tool" and detail.preview.text
            ),
            None,
        )
        tool_name = normalize_label(tool_name)
        if tool_name == AGENT_TELEMETRY_UNKNOWN_LABEL:
            tool_name = None
        return cls(
            record_id=record.record_id,
            revision=record.revision,
            participant_id=record.participant_id,
            source_epoch=record.source_epoch,
            lane=record.lane,
            kind=record.kind,
            source=record.source,
            status=record.status,
            raw_index=record.raw_index,
            source_offset=record.source_offset,
            event_ordinal=record.event_ordinal,
            turn_id=record.turn_id,
            step_id=record.step_id,
            request_id=record.request_id,
            call_id=record.call_id,
            parent_call_id=record.parent_call_id,
            mcp_server=record.mcp_server,
            mcp_tool=record.mcp_tool,
            tool_name=tool_name,
            timing=record.timing,
            usage=record.usage,
            failure_category=record.failure.category.value if record.failure is not None else None,
            failure_code=record.failure.code if record.failure is not None else None,
            retry_of_record_id=record.retry_of_record_id,
            retry_attempt=record.retry_attempt,
        )

    def to_record(self) -> TrajectoryRecord:
        """Rebuild the minimal canonical projection without retained content."""
        failure = (
            TrajectoryFailure(
                category=TrajectoryFailureCategory(self.failure_category),
                code=self.failure_code,
            )
            if self.failure_category is not None
            else None
        )
        details = (
            (DetailField.from_text("tool", self.tool_name),) if self.tool_name is not None else ()
        )
        return TrajectoryRecord(
            record_id=self.record_id,
            revision=self.revision,
            participant_id=self.participant_id,
            source_epoch=self.source_epoch,
            lane=self.lane,
            kind=self.kind,
            source=self.source,
            summary="",
            status=self.status,
            raw_index=self.raw_index,
            source_offset=self.source_offset,
            event_ordinal=self.event_ordinal,
            turn_id=self.turn_id,
            step_id=self.step_id,
            request_id=self.request_id,
            call_id=self.call_id,
            parent_call_id=self.parent_call_id,
            mcp_server=self.mcp_server,
            mcp_tool=self.mcp_tool,
            timing=self.timing,
            usage=self.usage,
            failure=failure,
            retry_of_record_id=self.retry_of_record_id,
            retry_attempt=self.retry_attempt,
            details=details,
        )


@dataclass(frozen=True, slots=True)
class SpanContextState:
    """A real emitted span context and its honest absolute interval."""

    context: Any
    start_time_ns: int
    end_time_ns: int


@dataclass(slots=True)
class ParticipantEmissionState:
    """Bounded telemetry state for one participant and one source epoch."""

    participant_id: str
    source_epoch: str
    emitted: OrderedDict[tuple[str, str], None] = field(default_factory=OrderedDict)
    log_revisions: OrderedDict[str, int] = field(default_factory=OrderedDict)
    records: OrderedDict[str, CanonicalRecordSnapshot] = field(default_factory=OrderedDict)
    span_contexts: OrderedDict[tuple[str, str], SpanContextState] = field(
        default_factory=OrderedDict
    )


class AgentTelemetryState:
    """Bounds all participant-epoch state without retaining record content."""

    def __init__(self) -> None:
        self._participants: OrderedDict[tuple[str, str], ParticipantEmissionState] = OrderedDict()

    def for_participant(self, participant_id: str, source_epoch: str) -> ParticipantEmissionState:
        """Return the independent state for this participant and source epoch."""
        key = participant_id, source_epoch
        state = self._participants.get(key)
        if state is None:
            state = ParticipantEmissionState(participant_id, source_epoch)
            self._participants[key] = state
        self._participants.move_to_end(key)
        while len(self._participants) > AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT:
            self._participants.popitem(last=False)
        return state

    def discard(self, participant_id: str) -> None:
        """Forget every retained source epoch for a discarded participant stream."""
        for key in tuple(self._participants):
            if key[0] == participant_id:
                del self._participants[key]

    @staticmethod
    def merge_records(
        state: ParticipantEmissionState, records: tuple[TrajectoryRecord, ...]
    ) -> tuple[TrajectoryRecord, ...]:
        """Retain only higher revisions and return the newly accepted records."""
        accepted: list[TrajectoryRecord] = []
        for record in records:
            prior = state.records.get(record.record_id)
            if prior is not None and record.revision <= prior.revision:
                continue
            state.records[record.record_id] = CanonicalRecordSnapshot.from_record(record)
            state.records.move_to_end(record.record_id)
            accepted.append(record)
        while len(state.records) > AGENT_TELEMETRY_RECORD_SNAPSHOT_LIMIT:
            state.records.popitem(last=False)
        return tuple(accepted)

    @staticmethod
    def records_for_projection(state: ParticipantEmissionState) -> tuple[TrajectoryRecord, ...]:
        """Rehydrate only the metadata required by pure request and tool projections."""
        return tuple(snapshot.to_record() for snapshot in state.records.values())

    @staticmethod
    def needs_log(state: ParticipantEmissionState, record: TrajectoryRecord) -> bool:
        """Whether this record's current retained revision has not logged successfully."""
        current = state.records.get(record.record_id)
        return (
            current is not None
            and current.revision == record.revision
            and (state.log_revisions.get(record.record_id, -1) < record.revision)
        )

    @staticmethod
    def remember_log(state: ParticipantEmissionState, record_id: str, revision: int) -> None:
        """Remember one successfully emitted highest record revision."""
        state.log_revisions[record_id] = revision
        state.log_revisions.move_to_end(record_id)
        while len(state.log_revisions) > AGENT_TELEMETRY_LOG_REVISION_LIMIT:
            state.log_revisions.popitem(last=False)

    @staticmethod
    def contains(state: ParticipantEmissionState, signal_name: str, signal_id: str) -> bool:
        """Whether this metric or span signal was already emitted in this epoch."""
        key = signal_name, signal_id
        if key not in state.emitted:
            return False
        state.emitted.move_to_end(key)
        return True

    @staticmethod
    def remember(state: ParticipantEmissionState, signal_name: str, signal_id: str) -> None:
        """Remember one successfully emitted metric or span signal."""
        key = signal_name, signal_id
        state.emitted[key] = None
        state.emitted.move_to_end(key)
        while len(state.emitted) > AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT:
            state.emitted.popitem(last=False)

    @staticmethod
    def span_context(
        state: ParticipantEmissionState, kind: str, identity: str
    ) -> SpanContextState | None:
        """Return a retained real span context while refreshing its LRU position."""
        key = kind, identity
        value = state.span_contexts.get(key)
        if value is not None:
            state.span_contexts.move_to_end(key)
        return value

    @staticmethod
    def remember_span_context(
        state: ParticipantEmissionState,
        kind: str,
        identity: str,
        context: Any,
        interval: tuple[int, int],
    ) -> None:
        """Retain a real emitted request or tool context within a separate LRU bound."""
        key = kind, identity
        state.span_contexts[key] = SpanContextState(context, interval[0], interval[1])
        state.span_contexts.move_to_end(key)
        while len(state.span_contexts) > AGENT_TELEMETRY_SPAN_CONTEXT_LIMIT:
            state.span_contexts.popitem(last=False)


__all__ = [
    "AgentTelemetryState",
    "CanonicalRecordSnapshot",
    "ParticipantEmissionState",
    "SpanContextState",
]
