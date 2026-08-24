"""Additive, source-local trajectory facts emitted by harness adapters."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_MAX_LINKS_PER_RECORD,
    TRAJECTORY_SOURCE_MAX_BYTES,
)
from theater.harness.contracts.events import Event
from theater.trajectory.content import (
    ContentPreview,
    DetailField,
    bound_detail_fields,
    bounded_text,
)
from theater.trajectory.enums import (
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.records import Timing, TrajectoryUsage


@dataclass(frozen=True, slots=True)
class FactLink:
    """A harness-native link that is not a Theater participant claim."""

    target_id: str
    relation: str = "related"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_id",
            bounded_text(
                self.target_id,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="fact link target_id",
                nonempty=True,
            ),
        )
        object.__setattr__(
            self,
            "relation",
            bounded_text(
                self.relation,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="fact link relation",
                nonempty=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryFact:
    """Rich facts a source observed before daemon participant attribution."""

    kind: TrajectoryKind
    summary: str = ""
    source: str = "harness"
    lane: TrajectoryLane | None = None
    status: TrajectoryStatus = TrajectoryStatus.UNKNOWN
    native_id: str | None = None
    revision: int = 0
    raw_index: int = 0
    event_ordinal: int = 0
    turn_id: str | None = None
    step_id: str | None = None
    request_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    timing: Timing | None = None
    usage: TrajectoryUsage | None = None
    details: tuple[DetailField, ...] = ()
    links: tuple[FactLink, ...] = ()
    source_offset: int | None = None

    def __post_init__(self) -> None:
        _validate_fact_text(self)
        _validate_fact_identity(self)
        _validate_fact_payload(self)
        object.__setattr__(self, "details", bound_detail_fields(self.details))
        object.__setattr__(self, "links", tuple(self.links))
        _validate_fact_collections(self)


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    """The single-pass result shared by control and trajectory consumers."""

    events: Sequence[Event] = ()
    trajectory: Sequence[TrajectoryFact] = ()
    #: None projects all control events; an explicit sequence can suppress duplicate views.
    trajectory_events: Sequence[Event] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "trajectory", tuple(self.trajectory))
        if self.trajectory_events is not None:
            object.__setattr__(self, "trajectory_events", tuple(self.trajectory_events))
        if any(not isinstance(event, Event) for event in self.events):
            raise TrajectoryValidationError("parsed events must contain Event values")
        if any(not isinstance(fact, TrajectoryFact) for fact in self.trajectory):
            raise TrajectoryValidationError("parsed trajectory must contain TrajectoryFact values")
        if self.trajectory_events is not None and any(
            not isinstance(event, Event) for event in self.trajectory_events
        ):
            raise TrajectoryValidationError("trajectory events must contain Event values")

    @property
    def facts(self) -> tuple[TrajectoryFact, ...]:
        return tuple(self.trajectory)

    @property
    def baseline_events(self) -> tuple[Event, ...]:
        return tuple(self.events if self.trajectory_events is None else self.trajectory_events)


def _enum(enum_type: type, value: object, label: str):
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TrajectoryValidationError(f"{label} must be a valid enum value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise TrajectoryValidationError(f"{label} has unknown value {value!r}") from exc


def _validate_fact_text(fact: TrajectoryFact) -> None:
    object.__setattr__(
        fact,
        "source",
        bounded_text(
            fact.source,
            max_bytes=TRAJECTORY_SOURCE_MAX_BYTES,
            label="fact.source",
            nonempty=True,
        ),
    )
    if not isinstance(fact.summary, str):
        raise TrajectoryValidationError("fact.summary must be a string")
    object.__setattr__(fact, "summary", ContentPreview.from_text(fact.summary).text)
    object.__setattr__(fact, "kind", _enum(TrajectoryKind, fact.kind, "fact.kind"))
    if fact.lane is not None:
        object.__setattr__(fact, "lane", _enum(TrajectoryLane, fact.lane, "fact.lane"))
    object.__setattr__(fact, "status", _enum(TrajectoryStatus, fact.status, "fact.status"))


def _validate_fact_identity(fact: TrajectoryFact) -> None:
    if fact.native_id is not None:
        object.__setattr__(
            fact,
            "native_id",
            bounded_text(
                fact.native_id,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="fact.native_id",
                nonempty=True,
            ),
        )
    if type(fact.revision) is not int or fact.revision < 0:
        raise TrajectoryValidationError("fact.revision must be a non-negative integer")
    if fact.source_offset is not None and (
        type(fact.source_offset) is not int or fact.source_offset < 0
    ):
        raise TrajectoryValidationError("fact.source_offset must be a non-negative integer or null")
    for name in ("raw_index", "event_ordinal"):
        value = getattr(fact, name)
        if type(value) is not int or value < 0:
            raise TrajectoryValidationError(f"fact.{name} must be a non-negative integer")
    for name in ("turn_id", "step_id", "request_id", "call_id", "parent_call_id"):
        value = getattr(fact, name)
        if value is not None:
            object.__setattr__(
                fact,
                name,
                bounded_text(
                    value,
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label=f"fact.{name}",
                    nonempty=True,
                ),
            )


def _validate_fact_payload(fact: TrajectoryFact) -> None:
    if fact.timing is not None and not isinstance(fact.timing, Timing):
        raise TrajectoryValidationError("fact.timing must be Timing or null")
    if fact.usage is not None and not isinstance(fact.usage, TrajectoryUsage):
        raise TrajectoryValidationError("fact.usage must be TrajectoryUsage or null")


def _validate_fact_collections(fact: TrajectoryFact) -> None:
    if any(not isinstance(value, DetailField) for value in fact.details):
        raise TrajectoryValidationError("fact.details must contain DetailField values")
    if any(not isinstance(value, FactLink) for value in fact.links):
        raise TrajectoryValidationError("fact.links must contain FactLink values")
    if len(fact.links) > TRAJECTORY_MAX_LINKS_PER_RECORD:
        raise TrajectoryValidationError(
            f"fact.links exceeds {TRAJECTORY_MAX_LINKS_PER_RECORD} values"
        )


__all__ = ["FactLink", "ParsedRecord", "TrajectoryFact"]
