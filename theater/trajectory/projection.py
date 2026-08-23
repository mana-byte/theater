"""Pure projections from normalized harness events and source-local facts."""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import (
    Timing,
    TrajectoryRecord,
    TrajectoryUsage,
)


def fallback_record_id(
    source_epoch: str,
    raw_index: int,
    event_ordinal: int,
    *,
    source_offset: int | None = None,
) -> str:
    """Build the stable baseline identity from trusted source-local coordinates."""
    if not isinstance(source_epoch, str) or not source_epoch:
        raise ValueError("source_epoch must be non-empty")
    if type(raw_index) is not int or raw_index < 0:
        raise ValueError("raw_index must be a non-negative integer")
    if type(event_ordinal) is not int or event_ordinal < 0:
        raise ValueError("event_ordinal must be a non-negative integer")
    if source_offset is not None and (type(source_offset) is not int or source_offset < 0):
        raise ValueError("source_offset must be a non-negative integer or null")
    coordinate = raw_index if source_offset is None else source_offset
    return _bounded_identity(f"{source_epoch}:{coordinate}:{event_ordinal}", "fallback")


def record_id_for_fact(fact: TrajectoryFact, source_epoch: str) -> str:
    """Namespace local native ids and use source coordinates for fallbacks."""
    if fact.native_id is not None:
        return _namespaced_native_id(fact.native_id, source_epoch)
    return fallback_record_id(
        source_epoch,
        fact.raw_index,
        fact.event_ordinal,
        source_offset=fact.source_offset,
    )


def event_to_fact(
    event: Event,
    *,
    source: str = "baseline",
    event_ordinal: int = 0,
) -> TrajectoryFact:
    """Project one normalized control event without assigning a participant."""
    kind, lane = _kind_and_lane(event.kind)
    status = TrajectoryStatus.ERROR if event.kind is EventKind.ERROR else TrajectoryStatus.COMPLETED
    timing = (
        Timing(start=event.ts, provenance=TimingProvenance.SOURCE) if event.ts is not None else None
    )
    usage = _usage(event)
    details: list[DetailField] = []
    if event.tool_name:
        details.append(DetailField.from_text("tool", event.tool_name, format=ContentFormat.TEXT))
    if event.raw_text is not None and event.raw_text != event.text:
        details.append(DetailField.from_text("raw", event.raw_text, format=ContentFormat.TEXT))
    return TrajectoryFact(
        kind=kind,
        lane=lane,
        source=source,
        summary=event.text,
        status=status,
        raw_index=event.raw_index,
        source_offset=event.source_offset,
        event_ordinal=event_ordinal,
        turn_id=event.turn_id,
        timing=timing,
        usage=usage,
        details=tuple(details),
    )


def fact_to_record(
    fact: TrajectoryFact,
    *,
    participant_id: str,
    source_epoch: str,
    source: str | None = None,
    links: Iterable = (),
) -> TrajectoryRecord:
    """Add daemon-supplied participant identity to a source-local fact."""
    lane = fact.lane or _lane_for_kind(fact.kind)
    return TrajectoryRecord(
        record_id=record_id_for_fact(fact, source_epoch),
        revision=fact.revision,
        participant_id=participant_id,
        source_epoch=source_epoch,
        lane=lane,
        kind=fact.kind,
        source=source or fact.source,
        summary=fact.summary,
        status=fact.status,
        native_id=(
            _namespaced_native_id(fact.native_id, source_epoch)
            if fact.native_id is not None
            else None
        ),
        raw_index=fact.raw_index,
        source_offset=fact.source_offset,
        event_ordinal=fact.event_ordinal,
        turn_id=fact.turn_id,
        step_id=fact.step_id,
        call_id=fact.call_id,
        parent_call_id=fact.parent_call_id,
        links=tuple(links),
        timing=fact.timing,
        usage=fact.usage,
        details=fact.details,
    )


def event_to_record(
    event: Event,
    *,
    participant_id: str,
    source_epoch: str,
    event_ordinal: int = 0,
    source: str = "baseline",
) -> TrajectoryRecord:
    """Project one normalized Event into a canonical trajectory record."""
    return fact_to_record(
        event_to_fact(
            event,
            source=source,
            event_ordinal=event_ordinal,
        ),
        participant_id=participant_id,
        source_epoch=source_epoch,
    )


def project_events(
    events: Iterable[Event],
    *,
    participant_id: str,
    source_epoch: str,
    source: str = "baseline",
) -> tuple[TrajectoryRecord, ...]:
    """Project events while assigning ordinals only within each raw record."""
    ordinals: dict[int, int] = {}
    records: list[TrajectoryRecord] = []
    for event in events:
        coordinate = event.source_offset if event.source_offset is not None else event.raw_index
        ordinal = ordinals.get(coordinate, 0)
        ordinals[coordinate] = ordinal + 1
        records.append(
            event_to_record(
                event,
                participant_id=participant_id,
                source_epoch=source_epoch,
                event_ordinal=ordinal,
                source=source,
            )
        )
    return tuple(records)


def project_facts(
    facts: Iterable[TrajectoryFact],
    *,
    participant_id: str,
    source_epoch: str,
    source: str | None = None,
) -> tuple[TrajectoryRecord, ...]:
    """Project rich facts while leaving participant-link resolution to the daemon."""
    return tuple(
        fact_to_record(
            fact,
            participant_id=participant_id,
            source_epoch=source_epoch,
            source=source,
        )
        for fact in facts
    )


def _kind_and_lane(kind: EventKind) -> tuple[TrajectoryKind, TrajectoryLane]:
    mapping = {
        EventKind.USER: (TrajectoryKind.USER, TrajectoryLane.INPUT),
        EventKind.ASSISTANT: (TrajectoryKind.ASSISTANT, TrajectoryLane.MODEL),
        EventKind.TOOL_CALL: (TrajectoryKind.TOOL_CALL, TrajectoryLane.TOOLS),
        EventKind.TOOL_RESULT: (TrajectoryKind.TOOL_RESULT, TrajectoryLane.TOOLS),
        EventKind.ERROR: (TrajectoryKind.ERROR, TrajectoryLane.THEATER),
    }
    return mapping[kind]


def _lane_for_kind(kind: TrajectoryKind) -> TrajectoryLane:
    if kind is TrajectoryKind.USER:
        return TrajectoryLane.INPUT
    if kind in (
        TrajectoryKind.ASSISTANT,
        TrajectoryKind.REASONING,
        TrajectoryKind.SYSTEM,
        TrajectoryKind.CONTEXT,
    ):
        return TrajectoryLane.MODEL
    if kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        return TrajectoryLane.TOOLS
    return TrajectoryLane.THEATER


def _usage(event: Event) -> TrajectoryUsage | None:
    if event.usage is None:
        return None
    return TrajectoryUsage(
        model=event.usage.model,
        request_id=event.usage.idempotency_key,
        input_tokens=event.usage.input_tokens,
        output_tokens=event.usage.output_tokens,
        reasoning_tokens=event.usage.reasoning_output_tokens,
        cache_read_tokens=event.usage.cache_read_input_tokens,
        cache_write_tokens=event.usage.cache_creation_input_tokens,
        cost_usd=event.usage.cost_usd,
    )


def _namespaced_native_id(native_id: str, source_epoch: str) -> str:
    return _bounded_identity(f"{source_epoch}:{native_id}", "native")


def _bounded_identity(value: str, prefix: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("trajectory identity must contain valid UTF-8") from exc
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        raise ValueError("trajectory identity must not contain control characters")
    if len(encoded) <= TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return value
    return f"{prefix}:{sha256(encoded).hexdigest()}"


__all__ = [
    "event_to_fact",
    "event_to_record",
    "fact_to_record",
    "fallback_record_id",
    "project_events",
    "project_facts",
    "record_id_for_fact",
]
