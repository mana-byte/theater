"""Immutable trajectory values and their strict wire representations."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self, cast

from theater.constants.trajectory import (
    TRAJECTORY_DETAIL_FIELD_MAX_BYTES,
    TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
)


class TrajectoryValidationError(ValueError):
    """A trajectory value or wire object failed validation."""


class TrajectoryLane(StrEnum):
    INPUT = "input"
    MODEL = "model"
    TOOLS = "tools"
    THEATER = "theater"


class TrajectoryKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    SYSTEM = "system"
    CONTEXT = "context"
    THEATER = "theater"
    SPAWN = "spawn"
    RESUME = "resume"
    SEND = "send"
    RECEIVE = "receive"
    AWAIT_START = "await_start"
    AWAIT_END = "await_end"
    KILL = "kill"
    JOB_FAILURE = "job_failure"
    TRANSCRIPT_BOUNDARY = "transcript_boundary"
    SESSION_BOUNDARY = "session_boundary"
    OBSERVATION_ERROR = "observation_error"
    UNKNOWN = "unknown"


class TrajectoryStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class ContentFormat(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    JSON = "json"
    CODE = "code"
    DIFF = "diff"
    PATH = "path"
    IMAGE = "image"
    BINARY = "binary"


class TimingProvenance(StrEnum):
    SOURCE = "source"
    OBSERVED = "observed"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


class LinkDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    RELATED = "related"


class GroupKind(StrEnum):
    TURN = "turn"
    STEP = "step"
    BETWEEN_TURNS = "between_turns"


class PanelState(StrEnum):
    READY = "ready"
    WAITING = "waiting"
    UNTRUSTED = "untrusted"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


def _safe_text(value: str) -> str:
    if not isinstance(value, str):
        raise TrajectoryValidationError("trajectory text must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TrajectoryValidationError("trajectory text must contain valid UTF-8") from exc
    escaped: list[str] = []
    for char in value:
        code = ord(char)
        if char == "\\":
            escaped.append("\\\\")
        elif char == "[":
            escaped.append("\\[")
        elif char in "\n\r\t":
            escaped.append(char)
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            escaped.append(f"\\x{code:02x}")
        else:
            escaped.append(char)
    return "".join(escaped)


def _is_rich_safe(value: str) -> bool:
    backslashes = 0
    for char in value:
        code = ord(char)
        if code < 0x20 and char not in "\n\r\t":
            return False
        if 0x7F <= code <= 0x9F:
            return False
        if char == "\\":
            backslashes += 1
            continue
        if char == "[" and backslashes % 2 == 0:
            return False
        backslashes = 0
    return True


def escape_rich_text(value: str) -> str:
    """Escape terminal controls and Rich markup without executing either."""
    return _safe_text(value)


def _clip_safe_text(value: str, max_bytes: int) -> tuple[str, int]:
    if max_bytes <= 0:
        raise TrajectoryValidationError("trajectory preview limit must be positive")
    data = value.encode("utf-8")
    total = len(data)
    if total <= max_bytes:
        return value, 0

    available = max_bytes
    for _ in range(8):
        omitted_guess = max(total - available, 0)
        marker = f"… {omitted_guess} bytes omitted …"
        marker_bytes = len(marker.encode("utf-8"))
        available = max(max_bytes - marker_bytes, 0)
        head_budget = available // 2
        tail_budget = available - head_budget
        head = data[:head_budget].decode("utf-8", errors="ignore")
        tail = data[-tail_budget:].decode("utf-8", errors="ignore") if tail_budget else ""
        shown = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
        omitted = total - shown
        marker = f"… {omitted} bytes omitted …"
        if len((head + marker + tail).encode("utf-8")) <= max_bytes:
            return head + marker + tail, omitted
        available = max(available - 1, 0)

    marker = f"… {total} bytes omitted …"
    if len(marker.encode("utf-8")) > max_bytes:
        marker = marker.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return marker, total


def _preview(value: str, *, max_bytes: int, prior_omitted: int = 0) -> ContentPreview:
    safe = _safe_text(value)
    return _preview_safe(safe, max_bytes=max_bytes, prior_omitted=prior_omitted)


def _preview_safe(value: str, *, max_bytes: int, prior_omitted: int = 0) -> ContentPreview:
    text, omitted = _clip_safe_text(value, max_bytes)
    return ContentPreview(text=text, omitted_bytes=prior_omitted + omitted)


@dataclass(frozen=True, slots=True)
class ContentPreview:
    text: str
    omitted_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TrajectoryValidationError("content preview text must be a string")
        if type(self.omitted_bytes) is not int or self.omitted_bytes < 0:
            raise TrajectoryValidationError(
                "content preview omitted_bytes must be a non-negative integer"
            )
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TrajectoryValidationError(
                "content preview text must contain valid UTF-8"
            ) from exc
        if not _is_rich_safe(self.text):
            raise TrajectoryValidationError("content preview text must be Rich and control safe")
        if len(encoded) > TRAJECTORY_DETAIL_FIELD_MAX_BYTES:
            raise TrajectoryValidationError(
                f"content preview exceeds {TRAJECTORY_DETAIL_FIELD_MAX_BYTES} encoded bytes"
            )

    @classmethod
    def from_text(
        cls, value: str, *, max_bytes: int = TRAJECTORY_DETAIL_FIELD_MAX_BYTES
    ) -> ContentPreview:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise TrajectoryValidationError("content preview max_bytes must be a positive integer")
        return _preview(value, max_bytes=min(max_bytes, TRAJECTORY_DETAIL_FIELD_MAX_BYTES))

    @property
    def encoded_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def to_wire(self) -> dict[str, object]:
        return {"text": self.text, "omitted_bytes": self.omitted_bytes}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "content preview")
        _keys(data, required={"text", "omitted_bytes"}, optional=set(), label="content preview")
        return cls(
            text=_string(data["text"], "content preview.text"),
            omitted_bytes=_integer(data["omitted_bytes"], "content preview.omitted_bytes"),
        )


@dataclass(frozen=True, slots=True)
class DetailField:
    name: str
    value: ContentPreview | str
    format: ContentFormat = ContentFormat.TEXT

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise TrajectoryValidationError("detail field name must be a non-empty string")
        try:
            self.name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TrajectoryValidationError("detail field name must contain valid UTF-8") from exc
        object.__setattr__(self, "name", _safe_text(self.name))
        if isinstance(self.value, str):
            object.__setattr__(self, "value", ContentPreview.from_text(self.value))
        elif not isinstance(self.value, ContentPreview):
            raise TrajectoryValidationError("detail field value must be ContentPreview or string")
        object.__setattr__(self, "format", _enum(ContentFormat, self.format, "detail field.format"))

    @classmethod
    def from_text(
        cls,
        name: str,
        value: str,
        *,
        format: ContentFormat = ContentFormat.TEXT,
    ) -> Self:
        return cls(name=name, value=ContentPreview.from_text(value), format=format)

    def to_wire(self) -> dict[str, object]:
        return {"name": self.name, "format": self.format.value, "value": self.preview.to_wire()}

    @property
    def preview(self) -> ContentPreview:
        return cast(ContentPreview, self.value)

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "detail field")
        _keys(data, required={"name", "format", "value"}, optional=set(), label="detail field")
        return cls(
            name=_string(data["name"], "detail field.name"),
            format=_enum(ContentFormat, data["format"], "detail field.format"),
            value=ContentPreview.from_wire(data["value"]),
        )


def bound_detail_fields(fields: Iterable[DetailField]) -> tuple[DetailField, ...]:
    """Apply field and aggregate byte bounds in stable input order."""
    bounded: list[DetailField] = []
    remaining = TRAJECTORY_DETAIL_RECORD_MAX_BYTES
    for field_value in fields:
        if not isinstance(field_value, DetailField):
            raise TrajectoryValidationError("record details must contain DetailField values")
        preview = field_value.preview
        if remaining <= 0:
            clipped = ContentPreview(
                text="", omitted_bytes=preview.encoded_bytes + preview.omitted_bytes
            )
        elif preview.encoded_bytes <= remaining:
            clipped = preview
        else:
            clipped = _preview_safe(
                preview.text,
                max_bytes=min(remaining, TRAJECTORY_DETAIL_FIELD_MAX_BYTES),
                prior_omitted=preview.omitted_bytes,
            )
        bounded.append(DetailField(field_value.name, clipped, field_value.format))
        remaining -= clipped.encoded_bytes
    return tuple(bounded)


@dataclass(frozen=True, slots=True)
class Timing:
    start: float | None = None
    end: float | None = None
    duration_ms: float | None = None
    provenance: TimingProvenance = TimingProvenance.UNAVAILABLE

    def __post_init__(self) -> None:
        for name, value in (
            ("start", self.start),
            ("end", self.end),
            ("duration_ms", self.duration_ms),
        ):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise TrajectoryValidationError(f"timing.{name} must be a finite number or null")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise TrajectoryValidationError("timing.duration_ms must be non-negative")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise TrajectoryValidationError("timing.end must not precede timing.start")
        object.__setattr__(
            self, "provenance", _enum(TimingProvenance, self.provenance, "timing.provenance")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "duration_ms": self.duration_ms,
            "provenance": self.provenance.value,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "timing")
        _keys(
            data,
            required=set(),
            optional={"start", "end", "duration_ms", "provenance"},
            label="timing",
        )
        return cls(
            start=_number_or_none(data.get("start"), "timing.start"),
            end=_number_or_none(data.get("end"), "timing.end"),
            duration_ms=_number_or_none(data.get("duration_ms"), "timing.duration_ms"),
            provenance=_enum(
                TimingProvenance,
                data.get("provenance", TimingProvenance.UNAVAILABLE.value),
                "timing.provenance",
            ),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryUsage:
    model: str | None = None
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        for name in ("model", "request_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TrajectoryValidationError(f"usage.{name} must be a string or null")
            if value == "":
                raise TrajectoryValidationError(f"usage.{name} must be non-empty or null")
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TrajectoryValidationError(f"usage.{name} must be a non-negative integer")
        if self.cost_usd is not None and (
            type(self.cost_usd) not in (int, float)
            or not math.isfinite(self.cost_usd)
            or self.cost_usd < 0
        ):
            raise TrajectoryValidationError(
                "usage.cost_usd must be a non-negative finite number or null"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "model": self.model,
            "request_id": self.request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "usage")
        required = {
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        }
        _keys(
            data,
            required=required,
            optional={"model", "request_id", "cost_usd"},
            label="usage",
        )
        return cls(
            model=_string_or_none(data.get("model"), "usage.model"),
            request_id=_string_or_none(data.get("request_id"), "usage.request_id"),
            input_tokens=_integer(data["input_tokens"], "usage.input_tokens"),
            output_tokens=_integer(data["output_tokens"], "usage.output_tokens"),
            reasoning_tokens=_integer(data["reasoning_tokens"], "usage.reasoning_tokens"),
            cache_read_tokens=_integer(data["cache_read_tokens"], "usage.cache_read_tokens"),
            cache_write_tokens=_integer(data["cache_write_tokens"], "usage.cache_write_tokens"),
            cost_usd=_number_or_none(data.get("cost_usd"), "usage.cost_usd"),
        )


@dataclass(frozen=True, slots=True)
class ParticipantLink:
    participant_id: str
    relation: str
    direction: LinkDirection = LinkDirection.RELATED

    def __post_init__(self) -> None:
        if not isinstance(self.participant_id, str) or not self.participant_id:
            raise TrajectoryValidationError("participant link id must be a non-empty string")
        if not isinstance(self.relation, str) or not self.relation:
            raise TrajectoryValidationError("participant link relation must be a non-empty string")
        object.__setattr__(self, "relation", _safe_text(self.relation))
        object.__setattr__(
            self, "direction", _enum(LinkDirection, self.direction, "link.direction")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "participant_id": self.participant_id,
            "relation": self.relation,
            "direction": self.direction.value,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "participant link")
        _keys(
            data,
            required={"participant_id", "relation"},
            optional={"direction"},
            label="participant link",
        )
        return cls(
            participant_id=_string(data["participant_id"], "link.participant_id"),
            relation=_string(data["relation"], "link.relation"),
            direction=_enum(
                LinkDirection,
                data.get("direction", LinkDirection.RELATED.value),
                "link.direction",
            ),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryRecord:
    record_id: str
    revision: int
    participant_id: str
    source_epoch: str
    lane: TrajectoryLane
    kind: TrajectoryKind
    source: str
    summary: str
    status: TrajectoryStatus
    native_id: str | None = None
    raw_index: int = 0
    event_ordinal: int = 0
    turn_id: str | None = None
    step_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    links: tuple[ParticipantLink, ...] = ()
    timing: Timing | None = None
    usage: TrajectoryUsage | None = None
    details: tuple[DetailField, ...] = ()

    def __post_init__(self) -> None:
        for name in ("record_id", "participant_id", "source_epoch", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise TrajectoryValidationError(f"record.{name} must be a non-empty string")
        if type(self.revision) is not int or self.revision < 0:
            raise TrajectoryValidationError("record.revision must be a non-negative integer")
        for name in ("raw_index", "event_ordinal"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TrajectoryValidationError(f"record.{name} must be a non-negative integer")
        object.__setattr__(self, "lane", _enum(TrajectoryLane, self.lane, "record.lane"))
        object.__setattr__(self, "kind", _enum(TrajectoryKind, self.kind, "record.kind"))
        object.__setattr__(self, "status", _enum(TrajectoryStatus, self.status, "record.status"))
        object.__setattr__(self, "source", _safe_text(self.source))
        object.__setattr__(self, "summary", _safe_text(self.summary))
        for name in ("native_id", "turn_id", "step_id", "call_id", "parent_call_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise TrajectoryValidationError(f"record.{name} must be a non-empty string or null")
        object.__setattr__(self, "links", tuple(self.links))
        if any(not isinstance(link, ParticipantLink) for link in self.links):
            raise TrajectoryValidationError("record.links must contain ParticipantLink values")
        if self.timing is not None and not isinstance(self.timing, Timing):
            raise TrajectoryValidationError("record.timing must be Timing or null")
        if self.usage is not None and not isinstance(self.usage, TrajectoryUsage):
            raise TrajectoryValidationError("record.usage must be TrajectoryUsage or null")
        object.__setattr__(self, "details", bound_detail_fields(self.details))

    def to_wire(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "revision": self.revision,
            "participant_id": self.participant_id,
            "source_epoch": self.source_epoch,
            "lane": self.lane.value,
            "kind": self.kind.value,
            "source": self.source,
            "summary": self.summary,
            "status": self.status.value,
            "native_id": self.native_id,
            "raw_index": self.raw_index,
            "event_ordinal": self.event_ordinal,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "call_id": self.call_id,
            "parent_call_id": self.parent_call_id,
            "links": [link.to_wire() for link in self.links],
            "timing": self.timing.to_wire() if self.timing else None,
            "usage": self.usage.to_wire() if self.usage else None,
            "details": [detail.to_wire() for detail in self.details],
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "trajectory record")
        required = {
            "record_id",
            "revision",
            "participant_id",
            "source_epoch",
            "lane",
            "kind",
            "source",
            "summary",
            "status",
        }
        optional = {
            "native_id",
            "raw_index",
            "event_ordinal",
            "turn_id",
            "step_id",
            "call_id",
            "parent_call_id",
            "links",
            "timing",
            "usage",
            "details",
        }
        _keys(data, required=required, optional=optional, label="trajectory record")
        return cls(
            record_id=_string(data["record_id"], "record.record_id"),
            revision=_integer(data["revision"], "record.revision"),
            participant_id=_string(data["participant_id"], "record.participant_id"),
            source_epoch=_string(data["source_epoch"], "record.source_epoch"),
            lane=_enum(TrajectoryLane, data["lane"], "record.lane"),
            kind=_enum(TrajectoryKind, data["kind"], "record.kind"),
            source=_string(data["source"], "record.source"),
            summary=_string(data["summary"], "record.summary"),
            status=_enum(TrajectoryStatus, data["status"], "record.status"),
            native_id=_string_or_none(data.get("native_id"), "record.native_id"),
            raw_index=_integer(data.get("raw_index", 0), "record.raw_index"),
            event_ordinal=_integer(data.get("event_ordinal", 0), "record.event_ordinal"),
            turn_id=_string_or_none(data.get("turn_id"), "record.turn_id"),
            step_id=_string_or_none(data.get("step_id"), "record.step_id"),
            call_id=_string_or_none(data.get("call_id"), "record.call_id"),
            parent_call_id=_string_or_none(data.get("parent_call_id"), "record.parent_call_id"),
            links=tuple(
                ParticipantLink.from_wire(item)
                for item in _sequence(data.get("links", []), "record.links")
            ),
            timing=Timing.from_wire(data["timing"]) if data.get("timing") is not None else None,
            usage=TrajectoryUsage.from_wire(data["usage"])
            if data.get("usage") is not None
            else None,
            details=tuple(
                DetailField.from_wire(item)
                for item in _sequence(data.get("details", []), "record.details")
            ),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryGroup:
    group_id: str
    kind: GroupKind
    label: str
    record_ids: tuple[str, ...] = ()
    children: tuple[Self, ...] = ()
    turn_id: str | None = None
    step_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, str) or not self.group_id:
            raise TrajectoryValidationError("group_id must be a non-empty string")
        if not isinstance(self.label, str):
            raise TrajectoryValidationError("group label must be a string")
        object.__setattr__(self, "kind", _enum(GroupKind, self.kind, "group.kind"))
        object.__setattr__(self, "label", _safe_text(self.label))
        object.__setattr__(self, "record_ids", tuple(self.record_ids))
        object.__setattr__(self, "children", tuple(self.children))
        if any(not isinstance(value, str) or not value for value in self.record_ids):
            raise TrajectoryValidationError("group.record_ids must contain non-empty strings")
        if any(not isinstance(value, TrajectoryGroup) for value in self.children):
            raise TrajectoryValidationError("group.children must contain TrajectoryGroup values")
        _string_or_none(self.turn_id, "group.turn_id")
        _string_or_none(self.step_id, "group.step_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "kind": self.kind.value,
            "label": self.label,
            "record_ids": list(self.record_ids),
            "children": [child.to_wire() for child in self.children],
            "turn_id": self.turn_id,
            "step_id": self.step_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "trajectory group")
        _keys(
            data,
            required={"group_id", "kind", "label"},
            optional={"record_ids", "children", "turn_id", "step_id"},
            label="trajectory group",
        )
        return cls(
            group_id=_string(data["group_id"], "group.group_id"),
            kind=_enum(GroupKind, data["kind"], "group.kind"),
            label=_string(data["label"], "group.label"),
            record_ids=tuple(
                _string(item, "group.record_ids[]")
                for item in _sequence(data.get("record_ids", []), "group.record_ids")
            ),
            children=tuple(
                TrajectoryGroup.from_wire(item)
                for item in _sequence(data.get("children", []), "group.children")
            ),
            turn_id=_string_or_none(data.get("turn_id"), "group.turn_id"),
            step_id=_string_or_none(data.get("step_id"), "group.step_id"),
        )


@dataclass(frozen=True, slots=True)
class CoverageGap:
    stream: str
    reason: str
    start: str | None = None
    end: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stream, str) or not self.stream:
            raise TrajectoryValidationError("coverage gap stream must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason:
            raise TrajectoryValidationError("coverage gap reason must be a non-empty string")
        object.__setattr__(self, "reason", _safe_text(self.reason))
        _string_or_none(self.start, "coverage gap.start")
        _string_or_none(self.end, "coverage gap.end")

    def to_wire(self) -> dict[str, object]:
        return {"stream": self.stream, "reason": self.reason, "start": self.start, "end": self.end}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "coverage gap")
        _keys(data, required={"stream", "reason"}, optional={"start", "end"}, label="coverage gap")
        return cls(
            stream=_string(data["stream"], "gap.stream"),
            reason=_string(data["reason"], "gap.reason"),
            start=_string_or_none(data.get("start"), "gap.start"),
            end=_string_or_none(data.get("end"), "gap.end"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryCoverage:
    transcript_floor: str | None = None
    theater_floor: str | None = None
    gaps: tuple[CoverageGap, ...] = ()

    def __post_init__(self) -> None:
        _string_or_none(self.transcript_floor, "coverage.transcript_floor")
        _string_or_none(self.theater_floor, "coverage.theater_floor")
        object.__setattr__(self, "gaps", tuple(self.gaps))
        if any(not isinstance(gap, CoverageGap) for gap in self.gaps):
            raise TrajectoryValidationError("coverage.gaps must contain CoverageGap values")

    def to_wire(self) -> dict[str, object]:
        return {
            "transcript_floor": self.transcript_floor,
            "theater_floor": self.theater_floor,
            "gaps": [gap.to_wire() for gap in self.gaps],
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "trajectory coverage")
        _keys(
            data,
            required=set(),
            optional={"transcript_floor", "theater_floor", "gaps"},
            label="trajectory coverage",
        )
        return cls(
            transcript_floor=_string_or_none(
                data.get("transcript_floor"), "coverage.transcript_floor"
            ),
            theater_floor=_string_or_none(data.get("theater_floor"), "coverage.theater_floor"),
            gaps=tuple(
                CoverageGap.from_wire(item)
                for item in _sequence(data.get("gaps", []), "coverage.gaps")
            ),
        )


@dataclass(frozen=True, slots=True)
class PanelStateInfo:
    state: PanelState
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _enum(PanelState, self.state, "panel state"))
        object.__setattr__(self, "message", _safe_text(self.message))

    def to_wire(self) -> dict[str, object]:
        return {"state": self.state.value, "message": self.message}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "panel state")
        _keys(data, required={"state"}, optional={"message"}, label="panel state")
        return cls(
            state=_enum(PanelState, data["state"], "panel state.state"),
            message=_string(data.get("message", ""), "panel state.message"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryPage:
    panel_state: PanelStateInfo
    stream_id: str | None = None
    cursor: str | None = None
    records: tuple[TrajectoryRecord, ...] = ()
    groups: tuple[TrajectoryGroup, ...] = ()
    older_cursor: str | None = None
    has_older: bool = False
    coverage: TrajectoryCoverage = field(default_factory=TrajectoryCoverage)
    truncated_by_bytes: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.panel_state, PanelStateInfo):
            raise TrajectoryValidationError("page.panel_state must be PanelStateInfo")
        _string_or_none(self.stream_id, "page.stream_id")
        _string_or_none(self.cursor, "page.cursor")
        _string_or_none(self.older_cursor, "page.older_cursor")
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "groups", tuple(self.groups))
        if any(not isinstance(record, TrajectoryRecord) for record in self.records):
            raise TrajectoryValidationError("page.records must contain TrajectoryRecord values")
        if any(not isinstance(group, TrajectoryGroup) for group in self.groups):
            raise TrajectoryValidationError("page.groups must contain TrajectoryGroup values")
        if not isinstance(self.coverage, TrajectoryCoverage):
            raise TrajectoryValidationError("page.coverage must be TrajectoryCoverage")
        if type(self.has_older) is not bool or type(self.truncated_by_bytes) is not bool:
            raise TrajectoryValidationError("page boolean fields must be booleans")

    @property
    def state(self) -> PanelState:
        return self.panel_state.state

    def to_wire(self) -> dict[str, object]:
        return {
            "panel_state": self.panel_state.to_wire(),
            "stream_id": self.stream_id,
            "cursor": self.cursor,
            "records": [record.to_wire() for record in self.records],
            "groups": [group.to_wire() for group in self.groups],
            "older_cursor": self.older_cursor,
            "has_older": self.has_older,
            "coverage": self.coverage.to_wire(),
            "truncated_by_bytes": self.truncated_by_bytes,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "trajectory page")
        _keys(
            data,
            required={"panel_state"},
            optional={
                "stream_id",
                "cursor",
                "records",
                "groups",
                "older_cursor",
                "has_older",
                "coverage",
                "truncated_by_bytes",
            },
            label="trajectory page",
        )
        return cls(
            panel_state=PanelStateInfo.from_wire(data["panel_state"]),
            stream_id=_string_or_none(data.get("stream_id"), "page.stream_id"),
            cursor=_string_or_none(data.get("cursor"), "page.cursor"),
            records=tuple(
                TrajectoryRecord.from_wire(item)
                for item in _sequence(data.get("records", []), "page.records")
            ),
            groups=tuple(
                TrajectoryGroup.from_wire(item)
                for item in _sequence(data.get("groups", []), "page.groups")
            ),
            older_cursor=_string_or_none(data.get("older_cursor"), "page.older_cursor"),
            has_older=_boolean(data.get("has_older", False), "page.has_older"),
            coverage=TrajectoryCoverage.from_wire(data.get("coverage", {})),
            truncated_by_bytes=_boolean(
                data.get("truncated_by_bytes", False), "page.truncated_by_bytes"
            ),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryUpsert:
    record: TrajectoryRecord

    def __post_init__(self) -> None:
        if not isinstance(self.record, TrajectoryRecord):
            raise TrajectoryValidationError("upsert.record must be TrajectoryRecord")

    def to_wire(self) -> dict[str, object]:
        return {"record": self.record.to_wire()}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "trajectory upsert")
        _keys(data, required={"record"}, optional=set(), label="trajectory upsert")
        return cls(record=TrajectoryRecord.from_wire(data["record"]))


@dataclass(frozen=True, slots=True)
class TrajectoryDelta:
    stream_id: str
    cursor: str | None = None
    upserts: tuple[TrajectoryUpsert, ...] = ()
    resync_required: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, str) or not self.stream_id:
            raise TrajectoryValidationError("delta.stream_id must be a non-empty string")
        _string_or_none(self.cursor, "delta.cursor")
        object.__setattr__(self, "upserts", tuple(self.upserts))
        if any(not isinstance(upsert, TrajectoryUpsert) for upsert in self.upserts):
            raise TrajectoryValidationError("delta.upserts must contain TrajectoryUpsert values")
        if type(self.resync_required) is not bool:
            raise TrajectoryValidationError("delta.resync_required must be a boolean")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TrajectoryValidationError("delta.reason must be a string or null")
        if self.reason is not None:
            object.__setattr__(self, "reason", _safe_text(self.reason))

    def to_wire(self) -> dict[str, object]:
        return {
            "stream_id": self.stream_id,
            "cursor": self.cursor,
            "upserts": [upsert.to_wire() for upsert in self.upserts],
            "resync_required": self.resync_required,
            "reason": self.reason,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = _mapping(value, "trajectory delta")
        _keys(
            data,
            required={"stream_id"},
            optional={"cursor", "upserts", "resync_required", "reason"},
            label="trajectory delta",
        )
        return cls(
            stream_id=_string(data["stream_id"], "delta.stream_id"),
            cursor=_string_or_none(data.get("cursor"), "delta.cursor"),
            upserts=tuple(
                TrajectoryUpsert.from_wire(item)
                for item in _sequence(data.get("upserts", []), "delta.upserts")
            ),
            resync_required=_boolean(data.get("resync_required", False), "delta.resync_required"),
            reason=_string_or_none(data.get("reason"), "delta.reason"),
        )


def _enum(enum_type: type[StrEnum], value: object, label: str) -> Any:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TrajectoryValidationError(f"{label} must be a valid {enum_type.__name__} value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise TrajectoryValidationError(f"{label} has unknown value {value!r}") from exc


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TrajectoryValidationError(f"{label} must be an object with string keys")
    return value


def _keys(
    value: Mapping[str, object], *, required: set[str], optional: set[str], label: str
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise TrajectoryValidationError(f"{label} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise TrajectoryValidationError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TrajectoryValidationError(f"{label} must be a string")
    return value


def _string_or_none(value: object, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TrajectoryValidationError(f"{label} must be a string or null")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TrajectoryValidationError(f"{label} must be an integer")
    return value


def _number_or_none(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TrajectoryValidationError(f"{label} must be a finite number or null")
    return float(value)


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TrajectoryValidationError(f"{label} must be a boolean")
    return value


def _sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TrajectoryValidationError(f"{label} must be an array")
    return value


Lane = TrajectoryLane
Kind = TrajectoryKind
Status = TrajectoryStatus
Format = ContentFormat
Usage = TrajectoryUsage
RecordKind = TrajectoryKind
RecordStatus = TrajectoryStatus
TrajectoryDetailField = DetailField
TrajectoryContentFormat = ContentFormat
TrajectoryDetail = DetailField
TrajectoryPanelState = PanelState
TrajectoryPanelStateInfo = PanelStateInfo
TrajectoryRecordKind = TrajectoryKind
TrajectoryRecordStatus = TrajectoryStatus
TrajectoryTiming = Timing
TrajectoryTimingProvenance = TimingProvenance

__all__ = [
    "ContentFormat",
    "ContentPreview",
    "CoverageGap",
    "DetailField",
    "Format",
    "GroupKind",
    "Kind",
    "Lane",
    "LinkDirection",
    "PanelState",
    "PanelStateInfo",
    "ParticipantLink",
    "RecordKind",
    "RecordStatus",
    "Status",
    "Timing",
    "TimingProvenance",
    "TrajectoryContentFormat",
    "TrajectoryCoverage",
    "TrajectoryDelta",
    "TrajectoryDetail",
    "TrajectoryDetailField",
    "TrajectoryGroup",
    "TrajectoryKind",
    "TrajectoryLane",
    "TrajectoryPage",
    "TrajectoryPanelState",
    "TrajectoryPanelStateInfo",
    "TrajectoryRecord",
    "TrajectoryRecordKind",
    "TrajectoryRecordStatus",
    "TrajectoryStatus",
    "TrajectoryTiming",
    "TrajectoryTimingProvenance",
    "TrajectoryUpsert",
    "TrajectoryUsage",
    "TrajectoryValidationError",
    "Usage",
    "bound_detail_fields",
    "escape_rich_text",
]
