"""Wire values and bounded, process-local trajectory state for régie."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from math import isfinite
from typing import Self

MAX_FIELD_BYTES = 16 * 1024
MAX_DETAIL_BYTES = 32 * 1024
MAX_PAGE_RECORDS = 200
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_LOADED_RECORDS = 2_000
MAX_LOADED_BYTES = 8 * 1024 * 1024
MAX_PARTICIPANT_STATES = 8
MAX_IDENTIFIER_BYTES = 256
MAX_SOURCE_BYTES = 256
MAX_DETAIL_FIELDS = 64
MAX_LINKS = 32
MIN_INSPECTOR_RATIO = 0.20
MAX_INSPECTOR_RATIO = 0.75


class WireDecodeError(ValueError):
    """A trajectory response did not match the bounded wire contract."""


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WireDecodeError(f"{path} must be an object")
    return value


def _string(value: object, path: str, *, limit: int = MAX_FIELD_BYTES) -> str:
    if not isinstance(value, str):
        raise WireDecodeError(f"{path} must be a string")
    if len(value.encode("utf-8")) > limit:
        raise WireDecodeError(f"{path} exceeds {limit} UTF-8 bytes")
    return value


def _bounded_string(value: object, path: str, *, limit: int = MAX_FIELD_BYTES) -> str:
    if not isinstance(value, str):
        raise WireDecodeError(f"{path} must be a string")
    return clip_utf8(value, limit)[0]


def _identifier(value: object, path: str, *, limit: int = MAX_IDENTIFIER_BYTES) -> str:
    result = _string(value, path, limit=limit)
    if not result:
        raise WireDecodeError(f"{path} must not be empty")
    return result


def _optional_string(value: object, path: str, *, limit: int = MAX_FIELD_BYTES) -> str | None:
    if value is None:
        return None
    return _string(value, path, limit=limit)


def _integer(value: object, path: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireDecodeError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise WireDecodeError(f"{path} is outside [{minimum}, {maximum}]")
    return value


def _optional_integer(
    value: object,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int | None:
    if value is None:
        return None
    return _integer(value, path, minimum=minimum, maximum=maximum)


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise WireDecodeError(f"{path} must be a boolean")
    return value


def _enum[EnumValue: Enum](enum_type: type[EnumValue], value: object, path: str) -> EnumValue:
    if not isinstance(value, str):
        raise WireDecodeError(f"{path} must be a string enum value")
    return enum_type(value)


def _optional_enum[EnumValue: Enum](
    enum_type: type[EnumValue], value: object, path: str, default: EnumValue
) -> EnumValue:
    if value is None:
        return default
    return _enum(enum_type, value, path)


def _json_safe(value: object, *, depth: int = 0) -> object:  # noqa: PLR0912
    if depth > 8:
        raise WireDecodeError("content JSON nesting exceeds 8 levels")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return clip_utf8(value)[0]
    if isinstance(value, int):
        if abs(value) > 2**63 - 1:
            raise WireDecodeError("content JSON integer is too large")
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise WireDecodeError("content JSON number must be finite")
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 128:
            raise WireDecodeError("content JSON array is too large")
        return [_json_safe(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise WireDecodeError("content JSON object is too large")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise WireDecodeError("content JSON keys must be strings")
            _string(key, "content JSON key", limit=MAX_IDENTIFIER_BYTES)
            result[key] = _json_safe(item, depth=depth + 1)
        return result
    raise WireDecodeError("content value must be JSON-compatible")


def _text_value(value: object, path: str) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float, list, tuple, dict)):
        try:
            return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise WireDecodeError(f"{path} is not JSON-compatible") from exc
    raise WireDecodeError(f"{path} must be text or bounded JSON")


def _prefix_by_bytes(value: str, budget: int) -> str:
    used = 0
    end = 0
    for index, char in enumerate(value):
        size = len(char.encode("utf-8"))
        if used + size > budget:
            break
        used += size
        end = index + 1
    return value[:end]


def _suffix_by_bytes(value: str, budget: int) -> str:
    used = 0
    start = len(value)
    for index in range(len(value) - 1, -1, -1):
        size = len(value[index].encode("utf-8"))
        if used + size > budget:
            break
        used += size
        start = index
    return value[start:]


def clip_utf8(value: str, limit: int = MAX_FIELD_BYTES) -> tuple[str, bool, int]:
    """Clip text by UTF-8 bytes while retaining both ends and reporting the source size."""
    if limit < 32:
        raise ValueError("UTF-8 clipping limit must leave room for its omission marker")
    original_bytes = len(value.encode("utf-8"))
    if original_bytes <= limit:
        return value, False, original_bytes
    marker = "… 0 bytes omitted …"
    while True:
        available = max(2, limit - len(marker.encode("utf-8")))
        head_budget = available // 2
        tail_budget = available - head_budget
        head = _prefix_by_bytes(value, head_budget)
        tail = _suffix_by_bytes(value, tail_budget)
        kept_bytes = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
        omitted = max(0, original_bytes - kept_bytes)
        next_marker = f"… {omitted} bytes omitted …"
        if next_marker == marker:
            clipped = head + marker + tail
            if len(clipped.encode("utf-8")) <= limit:
                return clipped, True, original_bytes
        marker = next_marker
        if len(marker.encode("utf-8")) >= limit:
            marker = "…"
            return marker, True, original_bytes


class _UnknownEnumMixin:
    @classmethod
    def _missing_(cls, value: object) -> Enum | None:
        if isinstance(value, str):
            return getattr(cls, "__members__", {}).get("UNKNOWN")
        return None


class Lane(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    INPUT = "input"
    MODEL = "model"
    TOOLS = "tools"
    THEATER = "theater"


class RecordKind(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    TURN = "turn"
    STEP = "step"
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    CONTEXT_CHANGE = "context_change"
    SPAWN = "spawn"
    RESUME = "resume"
    SEND = "send"
    RECEIVE = "receive"
    AWAIT_START = "await_start"
    AWAIT_END = "await_end"
    KILL = "kill"
    JOB_FAILURE = "job_failure"
    SESSION_BOUNDARY = "session_boundary"
    OBSERVATION_ERROR = "observation_error"


class RecordStatus(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


class ContentFormat(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    TEXT = "text"
    MARKDOWN = "markdown"
    JSON = "json"
    DIFF = "diff"
    RAW = "raw"
    IMAGE = "image"
    BINARY = "binary"


class TimingProvenance(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    EXACT = "exact"
    SOURCE = "source"
    MISSING = "missing"


class LinkDirection(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    FROM = "from"
    TO = "to"
    RELATED = "related"


class PanelStatus(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    LIVE = "live"
    DEAD = "dead"
    EXTERNAL = "external"
    MISSING = "missing"
    READY = "ready"
    LOADING = "loading"
    WAITING = "waiting"
    UNTRUSTED = "untrusted"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class OrderMode(StrEnum):
    ORDER = "order"
    DURATION = "duration"


class FocusRegion(StrEnum):
    TIMELINE = "timeline"
    LEDGER = "ledger"
    INSPECTOR = "inspector"


class InspectorTab(StrEnum):
    SUMMARY = "summary"
    OUTPUT = "output"
    REASONING = "reasoning"
    USAGE = "usage"
    TIMING = "timing"
    INPUT = "input"
    RESULT = "result"
    PREVIEW = "preview"
    RAW = "raw"
    SOURCE = "source"
    PAYLOAD = "payload"
    CURRENT = "current"
    PREVIOUS = "previous"
    DIFF = "diff"


@dataclass(frozen=True, slots=True)
class ContentPreview:
    """A display value bounded by UTF-8 bytes before it reaches Textual."""

    text: str
    format: ContentFormat = ContentFormat.TEXT
    truncated: bool = False
    original_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("ContentPreview.text must be a string")
        if not isinstance(self.format, ContentFormat):
            raise TypeError("ContentPreview.format must be ContentFormat")
        clipped, was_clipped, original = clip_utf8(self.text)
        object.__setattr__(self, "text", clipped)
        object.__setattr__(self, "truncated", self.truncated or was_clipped)
        if self.original_bytes is None:
            object.__setattr__(self, "original_bytes", original)
        elif self.original_bytes < len(clipped.encode("utf-8")):
            raise ValueError("original_bytes cannot be less than displayed bytes")
        elif self.original_bytes > len(clipped.encode("utf-8")):
            object.__setattr__(self, "truncated", True)

    @classmethod
    def from_wire(cls, value: object, path: str = "content") -> Self:
        if isinstance(value, str):
            text = value
            format_value: object = ContentFormat.TEXT.value
            truncated = False
            original_value: object = None
        elif isinstance(value, Mapping):
            raw = _mapping(value, path)
            format_value = raw.get("format", ContentFormat.TEXT.value)
            if "text" in raw:
                text_value = raw["text"]
            elif "value" in raw:
                text_value = raw["value"]
            else:
                raise WireDecodeError(f"{path} requires text or value")
            text = _text_value(text_value, f"{path}.text")
            truncated_value = raw.get("truncated", False)
            truncated = _boolean(truncated_value, f"{path}.truncated")
            original_value = raw.get("original_bytes")
        elif value is None or isinstance(value, (bool, int, float, list, tuple, dict)):
            text = _text_value(value, path)
            format_value = ContentFormat.JSON.value
            truncated = False
            original_value = None
        else:
            raise WireDecodeError(f"{path} must be text or bounded JSON")
        content_format = _enum(ContentFormat, format_value, f"{path}.format")
        clipped, was_clipped, original = clip_utf8(text)
        supplied_original = _optional_integer(original_value, f"{path}.original_bytes")
        return cls(
            text=clipped,
            format=content_format,
            truncated=truncated or was_clipped,
            original_bytes=supplied_original if supplied_original is not None else original,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "text": self.text,
            "format": self.format.value,
            "truncated": self.truncated,
            "original_bytes": self.original_bytes,
        }


@dataclass(frozen=True, slots=True)
class DetailField:
    """One named inspector field with a bounded value."""

    name: str
    value: ContentPreview

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise TypeError("detail field names must be non-empty strings")
        if len(self.name.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
            raise ValueError("detail field name is too large")
        if not isinstance(self.value, ContentPreview):
            raise TypeError("detail field value must be ContentPreview")

    @classmethod
    def from_wire(cls, name: object, value: object, path: str) -> Self:
        return cls(
            _string(name, f"{path}.name", limit=MAX_IDENTIFIER_BYTES),
            ContentPreview.from_wire(value, path),
        )

    def to_wire(self) -> dict[str, object]:
        return {"name": self.name, "value": self.value.to_wire()}


def _decode_details(value: object) -> tuple[DetailField, ...]:
    if value is None:
        return ()
    fields: list[DetailField] = []
    if isinstance(value, Mapping):
        if len(value) > MAX_DETAIL_FIELDS:
            raise WireDecodeError(f"details exceeds {MAX_DETAIL_FIELDS} fields")
        for name, field_value in value.items():
            fields.append(DetailField.from_wire(name, field_value, f"details[{name!r}]"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_DETAIL_FIELDS:
            raise WireDecodeError(f"details exceeds {MAX_DETAIL_FIELDS} fields")
        for index, item in enumerate(value):
            raw = _mapping(item, f"details[{index}]")
            if "name" not in raw:
                raise WireDecodeError(f"details[{index}] requires name")
            field_value = raw.get("value", raw.get("text"))
            if field_value is None and "value" not in raw and "text" not in raw:
                raise WireDecodeError(f"details[{index}] requires value or text")
            fields.append(DetailField.from_wire(raw["name"], field_value, f"details[{index}]"))
    else:
        raise WireDecodeError("details must be an object or array")
    return _bound_details(fields)


def _bound_details(fields: Sequence[DetailField]) -> tuple[DetailField, ...]:
    def size(field: DetailField) -> int:
        return len(field.name.encode("utf-8")) + 2 + len(field.value.text.encode("utf-8"))

    if sum(size(field) for field in fields) <= MAX_DETAIL_BYTES:
        return tuple(fields)
    remaining = MAX_DETAIL_BYTES
    bounded: list[DetailField] = []
    for detail in fields:
        name_bytes = len(detail.name.encode("utf-8")) + 2
        if remaining <= name_bytes:
            break
        budget = min(MAX_FIELD_BYTES, remaining - name_bytes)
        clipped, was_clipped, original = clip_utf8(detail.value.text, max(32, budget))
        if len(clipped.encode("utf-8")) > budget:
            clipped = _prefix_by_bytes(clipped, budget)
        value = ContentPreview(
            clipped,
            format=detail.value.format,
            truncated=detail.value.truncated or was_clipped,
            original_bytes=max(original, detail.value.original_bytes or 0),
        )
        bounded_field = DetailField(detail.name, value)
        bounded.append(bounded_field)
        remaining -= size(bounded_field)
        if was_clipped or remaining <= 0:
            break
    return tuple(bounded)


@dataclass(frozen=True, slots=True)
class Timing:
    """Timing facts supplied by a source; no duration is inferred here."""

    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    provenance: TimingProvenance = TimingProvenance.MISSING

    def __post_init__(self) -> None:
        for value in (self.started_at, self.finished_at):
            if value is not None and len(value.encode("utf-8")) > MAX_FIELD_BYTES:
                raise ValueError("timing timestamp is too large")
        if self.duration_ms is not None and not 0 <= self.duration_ms <= 2**63 - 1:
            raise ValueError("timing duration is invalid")
        if not isinstance(self.provenance, TimingProvenance):
            raise TypeError("timing provenance must be TimingProvenance")

    @classmethod
    def from_wire(cls, value: object, path: str = "timing") -> Self | None:
        if value is None:
            return None
        raw = _mapping(value, path)
        started = raw.get("started_at", raw.get("start"))
        finished = raw.get("finished_at", raw.get("end"))
        return cls(
            started_at=_optional_string(started, f"{path}.started_at"),
            finished_at=_optional_string(finished, f"{path}.finished_at"),
            duration_ms=_optional_integer(raw.get("duration_ms"), f"{path}.duration_ms"),
            provenance=_optional_enum(
                TimingProvenance,
                raw.get("provenance"),
                f"{path}.provenance",
                TimingProvenance.MISSING,
            ),
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"provenance": self.provenance.value}
        if self.started_at is not None:
            result["started_at"] = self.started_at
        if self.finished_at is not None:
            result["finished_at"] = self.finished_at
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        return result

    @property
    def supports_duration(self) -> bool:
        return self.duration_ms is not None and self.provenance in {
            TimingProvenance.EXACT,
            TimingProvenance.SOURCE,
        }


@dataclass(frozen=True, slots=True)
class Usage:
    """Explicit usage counters with no source-specific payload passthrough."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_microcents: int | None = None

    @classmethod
    def from_wire(cls, value: object, path: str = "usage") -> Self | None:
        if value is None:
            return None
        raw = _mapping(value, path)

        def counter(name: str, *aliases: str) -> int | None:
            for key in (name, *aliases):
                if key in raw:
                    return _optional_integer(raw[key], f"{path}.{key}")
            return None

        return cls(
            input_tokens=counter("input_tokens", "input"),
            output_tokens=counter("output_tokens"),
            reasoning_tokens=counter("reasoning_tokens", "reasoning_output_tokens"),
            cache_read_tokens=counter("cache_read_tokens", "cache_read"),
            cache_write_tokens=counter("cache_write_tokens", "cache_creation_input_tokens"),
            cost_microcents=counter("cost_microcents"),
        )

    def to_wire(self) -> dict[str, int]:
        names = (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_microcents",
        )
        return {
            name: value
            for name, value in ((name, getattr(self, name)) for name in names)
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class ParticipantLink:
    """A navigable participant relationship attached to a coordination record."""

    participant_id: str
    direction: LinkDirection = LinkDirection.RELATED
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.participant_id:
            raise ValueError("participant link id must not be empty")
        if len(self.participant_id.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
            raise ValueError("participant link id is too large")
        if self.label is not None and len(self.label.encode("utf-8")) > MAX_FIELD_BYTES:
            raise ValueError("participant link label is too large")

    @classmethod
    def from_wire(cls, value: object, path: str = "link") -> Self:
        raw = _mapping(value, path)
        return cls(
            participant_id=_identifier(raw.get("participant_id"), f"{path}.participant_id"),
            direction=_optional_enum(
                LinkDirection,
                raw.get("direction"),
                f"{path}.direction",
                LinkDirection.RELATED,
            ),
            label=_optional_string(raw.get("label"), f"{path}.label"),
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "participant_id": self.participant_id,
            "direction": self.direction.value,
        }
        if self.label is not None:
            result["label"] = self.label
        return result


@dataclass(frozen=True, slots=True)
class TrajectoryRecord:
    """Immutable canonical record used by the ledger and inspector."""

    record_id: str
    revision: int
    participant_id: str
    lane: Lane
    kind: RecordKind
    source: str
    summary: str
    status: RecordStatus
    source_epoch: str | None = None
    turn_id: str | None = None
    step_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    links: tuple[ParticipantLink, ...] = ()
    timing: Timing | None = None
    usage: Usage | None = None
    details: tuple[DetailField, ...] = ()
    source_order: int | None = None
    occurred_at: str | None = None

    def __post_init__(self) -> None:  # noqa: PLR0912
        for name in ("record_id", "participant_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise TypeError(f"{name} must be a non-empty string")
            if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
                raise ValueError(f"{name} is too large")
        if self.source_epoch is not None:
            if not isinstance(self.source_epoch, str):
                raise TypeError("source_epoch must be a string")
            if len(self.source_epoch.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
                raise ValueError("source_epoch is too large")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise ValueError("revision must be a non-negative integer")
        if not isinstance(self.lane, Lane) or not isinstance(self.kind, RecordKind):
            raise TypeError("record lane and kind must be trajectory enums")
        if not isinstance(self.status, RecordStatus):
            raise TypeError("record status must be RecordStatus")
        if not isinstance(self.source, str):
            raise TypeError("record source must be a string")
        if not isinstance(self.summary, str):
            raise TypeError("record summary must be a string")
        if len(self.source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("record source is too large")
        clipped, _, _ = clip_utf8(self.summary)
        object.__setattr__(self, "summary", clipped)
        if self.occurred_at is not None:
            if not isinstance(self.occurred_at, str):
                raise TypeError("record timestamp must be a string")
            if len(self.occurred_at.encode("utf-8")) > MAX_FIELD_BYTES:
                raise ValueError("record timestamp is too large")
        if len(self.links) > MAX_LINKS:
            raise ValueError(f"record links exceeds {MAX_LINKS}")
        if len(self.details) > MAX_DETAIL_FIELDS:
            raise ValueError(f"record details exceeds {MAX_DETAIL_FIELDS}")
        object.__setattr__(self, "details", _bound_details(self.details))

    @classmethod
    def from_wire(cls, value: object, path: str = "record") -> Self:
        raw = _mapping(value, path)
        required = (
            "record_id",
            "revision",
            "participant_id",
            "lane",
            "kind",
            "source",
            "summary",
            "status",
        )
        for key in required:
            if key not in raw:
                raise WireDecodeError(f"{path} requires {key}")
        links_value = raw.get("links", ())
        if not isinstance(links_value, Sequence) or isinstance(
            links_value, (str, bytes, bytearray)
        ):
            raise WireDecodeError(f"{path}.links must be an array")
        if len(links_value) > MAX_LINKS:
            raise WireDecodeError(f"{path}.links exceeds {MAX_LINKS}")
        return cls(
            record_id=_identifier(raw["record_id"], f"{path}.record_id"),
            revision=_integer(raw["revision"], f"{path}.revision"),
            participant_id=_identifier(raw["participant_id"], f"{path}.participant_id"),
            source_epoch=_optional_string(
                raw.get("source_epoch", raw.get("session_epoch")),
                f"{path}.source_epoch",
                limit=MAX_IDENTIFIER_BYTES,
            ),
            lane=_enum(Lane, raw["lane"], f"{path}.lane"),
            kind=_enum(RecordKind, raw["kind"], f"{path}.kind"),
            source=_string(raw["source"], f"{path}.source", limit=MAX_SOURCE_BYTES),
            summary=_bounded_string(raw["summary"], f"{path}.summary"),
            status=_enum(RecordStatus, raw["status"], f"{path}.status"),
            turn_id=_optional_string(
                raw.get("turn_id"), f"{path}.turn_id", limit=MAX_IDENTIFIER_BYTES
            ),
            step_id=_optional_string(
                raw.get("step_id"), f"{path}.step_id", limit=MAX_IDENTIFIER_BYTES
            ),
            call_id=_optional_string(
                raw.get("call_id"), f"{path}.call_id", limit=MAX_IDENTIFIER_BYTES
            ),
            parent_call_id=_optional_string(
                raw.get("parent_call_id"), f"{path}.parent_call_id", limit=MAX_IDENTIFIER_BYTES
            ),
            links=tuple(
                ParticipantLink.from_wire(item, f"{path}.links[{index}]")
                for index, item in enumerate(links_value)
            ),
            timing=Timing.from_wire(raw.get("timing"), f"{path}.timing"),
            usage=Usage.from_wire(raw.get("usage"), f"{path}.usage"),
            details=_decode_details(raw.get("details", raw.get("detail"))),
            source_order=_optional_integer(raw.get("source_order"), f"{path}.source_order"),
            occurred_at=_optional_string(
                raw.get("occurred_at", raw.get("timestamp")), f"{path}.occurred_at"
            ),
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "record_id": self.record_id,
            "revision": self.revision,
            "participant_id": self.participant_id,
            "lane": self.lane.value,
            "kind": self.kind.value,
            "source": self.source,
            "summary": self.summary,
            "status": self.status.value,
        }
        optional = {
            "source_epoch": self.source_epoch,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "call_id": self.call_id,
            "parent_call_id": self.parent_call_id,
            "source_order": self.source_order,
            "occurred_at": self.occurred_at,
        }
        result.update({key: value for key, value in optional.items() if value is not None})
        if self.links:
            result["links"] = [link.to_wire() for link in self.links]
        if self.timing is not None:
            result["timing"] = self.timing.to_wire()
        if self.usage is not None:
            result["usage"] = self.usage.to_wire()
        if self.details:
            result["details"] = [field.to_wire() for field in self.details]
        return result

    @property
    def estimated_bytes(self) -> int:
        values = (
            self.record_id,
            self.participant_id,
            self.source,
            self.summary,
            self.source_epoch or "",
        )
        total = sum(len(value.encode("utf-8")) for value in values) + 64
        total += sum(
            len(field.name.encode("utf-8")) + len(field.value.text.encode("utf-8"))
            for field in self.details
        )
        total += sum(
            len(link.participant_id.encode("utf-8")) + len((link.label or "").encode("utf-8"))
            for link in self.links
        )
        if self.timing is not None:
            total += (
                sum(
                    len(value.encode("utf-8"))
                    for value in (self.timing.started_at or "", self.timing.finished_at or "")
                )
                + 32
            )
        if self.usage is not None:
            total += 8 * 24
        return total

    @property
    def group_id(self) -> str:
        if self.turn_id:
            return f"turn:{self.turn_id}"
        return "between-turns"


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """An honest source-history gap displayed by the panel."""

    source: str
    reason: str
    start: str | None = None
    end: str | None = None

    @classmethod
    def from_wire(cls, value: object, path: str = "gap") -> Self:
        raw = _mapping(value, path)
        return cls(
            source=_string(raw.get("source"), f"{path}.source", limit=MAX_SOURCE_BYTES),
            reason=_string(raw.get("reason"), f"{path}.reason"),
            start=_optional_string(raw.get("start"), f"{path}.start"),
            end=_optional_string(raw.get("end"), f"{path}.end"),
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"source": self.source, "reason": self.reason}
        if self.start is not None:
            result["start"] = self.start
        if self.end is not None:
            result["end"] = self.end
        return result


@dataclass(frozen=True, slots=True)
class Coverage:
    """Transcript and Theater-bus coverage floors and explicit gaps."""

    transcript_floor: str | None = None
    theater_floor: str | None = None
    gaps: tuple[CoverageGap, ...] = ()

    @classmethod
    def from_wire(cls, value: object, path: str = "coverage") -> Self:
        if value is None:
            return cls()
        raw = _mapping(value, path)
        gaps = raw.get("gaps", ())
        if not isinstance(gaps, Sequence) or isinstance(gaps, (str, bytes, bytearray)):
            raise WireDecodeError(f"{path}.gaps must be an array")
        if len(gaps) > 64:
            raise WireDecodeError(f"{path}.gaps exceeds 64")
        return cls(
            transcript_floor=_optional_string(
                raw.get("transcript_floor"), f"{path}.transcript_floor"
            ),
            theater_floor=_optional_string(raw.get("theater_floor"), f"{path}.theater_floor"),
            gaps=tuple(
                CoverageGap.from_wire(item, f"{path}.gaps[{index}]")
                for index, item in enumerate(gaps)
            ),
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.transcript_floor is not None:
            result["transcript_floor"] = self.transcript_floor
        if self.theater_floor is not None:
            result["theater_floor"] = self.theater_floor
        if self.gaps:
            result["gaps"] = [gap.to_wire() for gap in self.gaps]
        return result


@dataclass(frozen=True, slots=True)
class PanelInfo:
    """Panel availability and actionable prose supplied by the daemon."""

    status: PanelStatus
    message: str = ""
    retryable: bool = False

    @classmethod
    def from_wire(cls, value: object, path: str = "panel") -> Self:
        if isinstance(value, str):
            status = _enum(PanelStatus, value, path)
            return cls(status=status)
        raw = _mapping(value, path)
        status_value = raw.get("status", raw.get("state", "unknown"))
        return cls(
            status=_enum(PanelStatus, status_value, f"{path}.status"),
            message=_string(raw.get("message", ""), f"{path}.message"),
            retryable=_boolean(raw.get("retryable", False), f"{path}.retryable"),
        )

    def to_wire(self) -> dict[str, object]:
        return {"status": self.status.value, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True, slots=True)
class GroupMetadata:
    """Optional daemon grouping metadata; the client can derive a safe fallback."""

    group_id: str
    label: str
    record_ids: tuple[str, ...] = ()
    turn_id: str | None = None
    step_id: str | None = None

    @classmethod
    def from_wire(cls, value: object, path: str = "group") -> Self:
        raw = _mapping(value, path)
        ids = raw.get("record_ids", ())
        if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes, bytearray)):
            raise WireDecodeError(f"{path}.record_ids must be an array")
        if len(ids) > MAX_PAGE_RECORDS:
            raise WireDecodeError(f"{path}.record_ids exceeds {MAX_PAGE_RECORDS}")
        return cls(
            group_id=_identifier(raw.get("group_id"), f"{path}.group_id"),
            label=_string(raw.get("label"), f"{path}.label"),
            record_ids=tuple(
                _identifier(item, f"{path}.record_ids[{index}]") for index, item in enumerate(ids)
            ),
            turn_id=_optional_string(
                raw.get("turn_id"), f"{path}.turn_id", limit=MAX_IDENTIFIER_BYTES
            ),
            step_id=_optional_string(
                raw.get("step_id"), f"{path}.step_id", limit=MAX_IDENTIFIER_BYTES
            ),
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"group_id": self.group_id, "label": self.label}
        if self.record_ids:
            result["record_ids"] = list(self.record_ids)
        if self.turn_id is not None:
            result["turn_id"] = self.turn_id
        if self.step_id is not None:
            result["step_id"] = self.step_id
        return result


def _decode_page_records(value: object, path: str) -> tuple[tuple[TrajectoryRecord, ...], bool]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise WireDecodeError(f"{path} must be an array")
    if len(value) > MAX_PAGE_RECORDS:
        selected = value[:MAX_PAGE_RECORDS]
        truncated = True
    else:
        selected = value
        truncated = False
    records: list[TrajectoryRecord] = []
    byte_count = 0
    for index, item in enumerate(selected):
        record = TrajectoryRecord.from_wire(item, f"{path}[{index}]")
        next_size = byte_count + record.estimated_bytes
        if next_size > MAX_RESPONSE_BYTES:
            truncated = True
            break
        records.append(record)
        byte_count = next_size
    return tuple(records), truncated


@dataclass(frozen=True, slots=True)
class TrajectoryPage:
    """Decoded snapshot or one older-history page."""

    participant_id: str
    panel: PanelInfo
    stream_id: str | None
    cursor: str | None
    records: tuple[TrajectoryRecord, ...]
    older_cursor: str | None = None
    has_older: bool = False
    coverage: Coverage = field(default_factory=Coverage)
    groups: tuple[GroupMetadata, ...] = ()
    truncated_by_bytes: bool = False

    @classmethod
    def from_wire(
        cls, value: object, path: str = "snapshot", participant_id: str | None = None
    ) -> Self:
        raw = _mapping(value, path)
        participant_value = raw.get("participant_id", raw.get("id", participant_id))
        if participant_value is None:
            raise WireDecodeError(f"{path} requires participant_id")
        records, clipped = _decode_page_records(raw.get("records", ()), f"{path}.records")
        groups_value = raw.get("groups", ())
        if not isinstance(groups_value, Sequence) or isinstance(
            groups_value, (str, bytes, bytearray)
        ):
            raise WireDecodeError(f"{path}.groups must be an array")
        if len(groups_value) > MAX_PAGE_RECORDS:
            raise WireDecodeError(f"{path}.groups exceeds {MAX_PAGE_RECORDS}")
        return cls(
            participant_id=_identifier(participant_value, f"{path}.participant_id"),
            panel=PanelInfo.from_wire(
                raw.get("panel", raw.get("panel_state", raw.get("state", "unknown"))),
                f"{path}.panel",
            ),
            stream_id=_optional_string(
                raw.get("stream_id"), f"{path}.stream_id", limit=MAX_IDENTIFIER_BYTES
            ),
            cursor=_optional_string(
                raw.get("cursor"), f"{path}.cursor", limit=MAX_IDENTIFIER_BYTES
            ),
            records=records,
            older_cursor=_optional_string(
                raw.get("older_cursor"), f"{path}.older_cursor", limit=MAX_IDENTIFIER_BYTES
            ),
            has_older=_boolean(raw.get("has_older", False), f"{path}.has_older"),
            coverage=Coverage.from_wire(raw.get("coverage"), f"{path}.coverage"),
            groups=tuple(
                GroupMetadata.from_wire(item, f"{path}.groups[{index}]")
                for index, item in enumerate(groups_value)
            ),
            truncated_by_bytes=clipped
            or _boolean(raw.get("truncated_by_bytes", False), f"{path}.truncated_by_bytes"),
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "participant_id": self.participant_id,
            "panel": self.panel.to_wire(),
            "records": [record.to_wire() for record in self.records],
            "has_older": self.has_older,
            "truncated_by_bytes": self.truncated_by_bytes,
        }
        for key, value in (
            ("stream_id", self.stream_id),
            ("cursor", self.cursor),
            ("older_cursor", self.older_cursor),
        ):
            if value is not None:
                result[key] = value
        if self.groups:
            result["groups"] = [group.to_wire() for group in self.groups]
        if self.coverage != Coverage():
            result["coverage"] = self.coverage.to_wire()
        return result


@dataclass(frozen=True, slots=True)
class TrajectoryFollow:
    """Decoded ordinary request/reply follow result."""

    participant_id: str
    stream_id: str | None
    cursor: str | None
    upserts: tuple[TrajectoryRecord, ...] = ()
    resync_required: bool = False
    reason: str = ""
    panel: PanelInfo | None = None

    @classmethod
    def from_wire(
        cls, value: object, path: str = "follow", participant_id: str | None = None
    ) -> Self:
        raw = _mapping(value, path)
        participant_value = raw.get("participant_id", raw.get("id", participant_id))
        if participant_value is None:
            raise WireDecodeError(f"{path} requires participant_id")
        upserts, _ = _decode_page_records(
            raw.get("upserts", raw.get("records", ())), f"{path}.upserts"
        )
        return cls(
            participant_id=_identifier(participant_value, f"{path}.participant_id"),
            stream_id=_optional_string(
                raw.get("stream_id"), f"{path}.stream_id", limit=MAX_IDENTIFIER_BYTES
            ),
            cursor=_optional_string(
                raw.get("cursor", raw.get("new_cursor")),
                f"{path}.cursor",
                limit=MAX_IDENTIFIER_BYTES,
            ),
            upserts=upserts,
            resync_required=_boolean(raw.get("resync_required", False), f"{path}.resync_required"),
            reason=_string(raw.get("reason", ""), f"{path}.reason"),
            panel=PanelInfo.from_wire(raw["panel"], f"{path}.panel") if "panel" in raw else None,
        )

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "participant_id": self.participant_id,
            "upserts": [record.to_wire() for record in self.upserts],
            "resync_required": self.resync_required,
            "reason": self.reason,
        }
        if self.stream_id is not None:
            result["stream_id"] = self.stream_id
        if self.cursor is not None:
            result["cursor"] = self.cursor
        if self.panel is not None:
            result["panel"] = self.panel.to_wire()
        return result


FollowDelta = TrajectoryFollow
Record = TrajectoryRecord


def _record_size(record: TrajectoryRecord) -> int:
    return record.estimated_bytes


@dataclass(slots=True)
class ParticipantTrajectoryState:
    """Bounded mutable UI state for one participant; never persisted."""

    participant_id: str
    panel: PanelInfo = field(default_factory=lambda: PanelInfo(PanelStatus.LOADING))
    stream_id: str | None = None
    cursor: str | None = None
    older_cursor: str | None = None
    has_older: bool = False
    coverage: Coverage = field(default_factory=Coverage)
    groups: tuple[GroupMetadata, ...] = ()
    records: OrderedDict[str, TrajectoryRecord] = field(default_factory=OrderedDict)
    loaded_bytes: int = 0
    follow_tail: bool = True
    new_count: int = 0
    selected_id: str | None = None
    hovered_id: str | None = None
    collapsed_groups: set[str] = field(default_factory=set)
    query: str = ""
    lane_filters: set[Lane] = field(default_factory=set)
    kind_filters: set[RecordKind] = field(default_factory=set)
    status_filters: set[RecordStatus] = field(default_factory=set)
    source_filters: set[str] = field(default_factory=set)
    order_mode: OrderMode = OrderMode.ORDER
    timeline_scroll: int = 0
    inspector_tab: InspectorTab = InspectorTab.SUMMARY
    inspector_ratio: float = 0.35
    inspector_maximized: bool = False
    inspector_open: bool = False
    focus_region: FocusRegion = FocusRegion.LEDGER
    stale: bool = False
    stale_message: str = ""
    retry_kind: str | None = None
    retry_message: str = ""
    reload_required: bool = False
    truncated_by_bytes: bool = False
    loading_older: bool = False
    search_open: bool = False
    filters_open: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.participant_id, str) or not self.participant_id:
            raise ValueError("participant_id must be a non-empty string")
        if len(self.participant_id.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
            raise ValueError("participant_id is too large")
        self.inspector_ratio = max(
            MIN_INSPECTOR_RATIO, min(MAX_INSPECTOR_RATIO, self.inspector_ratio)
        )

    @property
    def record_list(self) -> list[TrajectoryRecord]:
        return list(self.records.values())

    @property
    def selected_record(self) -> TrajectoryRecord | None:
        return self.records.get(self.selected_id) if self.selected_id is not None else None

    @property
    def at_tail(self) -> bool:
        return self.follow_tail

    def _set_panel(self, panel: PanelInfo) -> None:
        self.panel = panel
        self.stale = panel.status == PanelStatus.STALE
        if not self.stale:
            self.stale_message = ""

    def _trim(self, *, evict_oldest: bool) -> None:
        while len(self.records) > MAX_LOADED_RECORDS or self.loaded_bytes > MAX_LOADED_BYTES:
            if not self.records:
                self.loaded_bytes = 0
                break
            record_id, record = self.records.popitem(last=evict_oldest)
            self.loaded_bytes -= _record_size(record)
            self.reload_required = True
            if record_id == self.selected_id:
                self.selected_id = None
            if record_id == self.hovered_id:
                self.hovered_id = None

    def upsert(
        self, records: Sequence[TrajectoryRecord], *, older: bool = False
    ) -> tuple[int, int]:
        """Apply records by stable ID and revision, returning (added, updated)."""
        added = 0
        updated = 0
        older_added: list[TrajectoryRecord] = []
        candidates: OrderedDict[str, TrajectoryRecord] = OrderedDict()
        for record in records:
            if record.participant_id == self.participant_id and (
                record.record_id not in candidates
                or record.revision > candidates[record.record_id].revision
            ):
                candidates[record.record_id] = record
        for record in candidates.values():
            existing = self.records.get(record.record_id)
            if existing is not None and record.revision <= existing.revision:
                continue
            if existing is not None:
                self.loaded_bytes -= _record_size(existing)
                self.records[record.record_id] = record
                updated += 1
            else:
                if older:
                    older_added.append(record)
                else:
                    self.records[record.record_id] = record
                added += 1
            self.loaded_bytes += _record_size(record)
        if older_added:
            self.records = OrderedDict(
                [(record.record_id, record) for record in older_added] + list(self.records.items())
            )
        self._trim(evict_oldest=older)
        if self.selected_id is None and self.records and self.follow_tail:
            self.selected_id = next(reversed(self.records))
        return added, updated

    def apply_snapshot(self, page: TrajectoryPage) -> None:
        """Replace loaded records with a validated snapshot."""
        if page.participant_id != self.participant_id:
            raise ValueError("snapshot participant does not match runtime state")
        prior_selection = self.selected_id
        self.records.clear()
        self.loaded_bytes = 0
        self.stream_id = page.stream_id
        self.cursor = page.cursor
        self.older_cursor = page.older_cursor
        self.has_older = page.has_older
        self.coverage = page.coverage
        self.groups = page.groups
        self.reload_required = False
        self.truncated_by_bytes = page.truncated_by_bytes
        self.loading_older = False
        self.retry_kind = None
        self.retry_message = ""
        self._set_panel(page.panel)
        self.upsert(page.records)
        if prior_selection in self.records:
            self.selected_id = prior_selection
        elif self.records:
            self.selected_id = next(reversed(self.records))
        else:
            self.selected_id = None

    def apply_older(self, page: TrajectoryPage) -> None:
        if page.participant_id != self.participant_id:
            raise ValueError("older page participant does not match runtime state")
        self.loading_older = False
        self.older_cursor = page.older_cursor
        self.has_older = page.has_older
        self.coverage = page.coverage
        known_groups = {group.group_id: group for group in self.groups}
        known_groups.update({group.group_id: group for group in page.groups})
        self.groups = tuple(known_groups.values())
        self.truncated_by_bytes = page.truncated_by_bytes
        self.retry_kind = None
        self.retry_message = ""
        self.upsert(page.records, older=True)

    def apply_follow(self, delta: TrajectoryFollow) -> tuple[int, int]:
        """Apply a follow delta without moving a paused tail."""
        if delta.participant_id != self.participant_id:
            raise ValueError("follow participant does not match runtime state")
        if delta.stream_id is not None:
            self.stream_id = delta.stream_id
        if delta.cursor is not None:
            self.cursor = delta.cursor
        if delta.panel is not None:
            self._set_panel(delta.panel)
        added, updated = self.upsert(delta.upserts)
        if added and not self.follow_tail:
            self.new_count += added
        elif added and self.follow_tail:
            self.new_count = 0
            self.selected_id = next(reversed(self.records))
        self.retry_kind = None
        self.retry_message = ""
        return added, updated

    def mark_stale(self, message: str) -> None:
        self.stale = True
        self.stale_message = message
        self.panel = PanelInfo(PanelStatus.STALE, message, retryable=True)
        self.retry_kind = "refresh"
        self.retry_message = message

    def mark_retry(self, kind: str, message: str) -> None:
        self.retry_kind = kind
        self.retry_message = message
        self.loading_older = False

    def mark_resync(self, message: str = "The trajectory stream changed; resync required.") -> None:
        self.reload_required = True
        self.mark_stale(message)
        self.retry_kind = "resync"

    def pause_follow(self) -> None:
        self.follow_tail = False

    def resume_follow(self) -> None:
        self.follow_tail = True
        self.new_count = 0
        if self.records:
            self.selected_id = next(reversed(self.records))

    def select(self, record_id: str | None) -> bool:
        if record_id is None:
            self.selected_id = None
            return True
        if record_id not in self.records:
            return False
        self.selected_id = record_id
        return True

    def move_selection(self, delta: int, visible_ids: Sequence[str] | None = None) -> str | None:
        ids = list(visible_ids) if visible_ids is not None else list(self.records)
        if not ids:
            self.selected_id = None
            return None
        current = (
            ids.index(self.selected_id)
            if self.selected_id in ids
            else (len(ids) - 1 if delta > 0 else 0)
        )
        target = max(0, min(len(ids) - 1, current + delta))
        self.selected_id = ids[target]
        if delta < 0:
            self.pause_follow()
        return self.selected_id

    def set_ratio(self, ratio: float) -> float:
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not isfinite(ratio):
            raise ValueError("inspector ratio must be finite")
        self.inspector_ratio = max(MIN_INSPECTOR_RATIO, min(MAX_INSPECTOR_RATIO, float(ratio)))
        return self.inspector_ratio

    def reset_ui(self) -> None:
        """Reset only this participant's in-memory presentation state."""
        self.query = ""
        self.lane_filters.clear()
        self.kind_filters.clear()
        self.status_filters.clear()
        self.source_filters.clear()
        self.collapsed_groups.clear()
        self.selected_id = next(reversed(self.records), None)
        self.hovered_id = None
        self.order_mode = OrderMode.ORDER
        self.timeline_scroll = 0
        self.inspector_tab = InspectorTab.SUMMARY
        self.inspector_maximized = False
        self.inspector_open = False
        self.focus_region = FocusRegion.LEDGER
        self.search_open = False
        self.filters_open = False
        self.follow_tail = True
        self.new_count = 0
        self.retry_kind = None
        self.retry_message = ""


class TrajectoryStateStore:
    """Small LRU of participant UI state with no persistence hooks."""

    def __init__(self, *, max_participants: int = MAX_PARTICIPANT_STATES) -> None:
        if max_participants < 1:
            raise ValueError("max_participants must be positive")
        self.max_participants = max_participants
        self._states: OrderedDict[str, ParticipantTrajectoryState] = OrderedDict()

    def get(self, participant_id: str) -> ParticipantTrajectoryState:
        if not isinstance(participant_id, str) or not participant_id:
            raise ValueError("participant_id must be a non-empty string")
        if len(participant_id.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
            raise ValueError("participant_id is too large")
        if participant_id in self._states:
            state = self._states.pop(participant_id)
            self._states[participant_id] = state
            return state
        state = ParticipantTrajectoryState(participant_id)
        self._states[participant_id] = state
        while len(self._states) > self.max_participants:
            self._states.popitem(last=False)
        return state

    def peek(self, participant_id: str) -> ParticipantTrajectoryState | None:
        return self._states.get(participant_id)

    def __len__(self) -> int:
        return len(self._states)

    def participant_ids(self) -> tuple[str, ...]:
        return tuple(self._states)


__all__ = [
    "MAX_DETAIL_BYTES",
    "MAX_FIELD_BYTES",
    "MAX_INSPECTOR_RATIO",
    "MAX_LOADED_BYTES",
    "MAX_LOADED_RECORDS",
    "MAX_PAGE_RECORDS",
    "MAX_RESPONSE_BYTES",
    "MIN_INSPECTOR_RATIO",
    "ContentFormat",
    "ContentPreview",
    "Coverage",
    "CoverageGap",
    "DetailField",
    "FocusRegion",
    "FollowDelta",
    "GroupMetadata",
    "InspectorTab",
    "Lane",
    "LinkDirection",
    "OrderMode",
    "PanelInfo",
    "PanelStatus",
    "ParticipantLink",
    "ParticipantTrajectoryState",
    "Record",
    "RecordKind",
    "RecordStatus",
    "Timing",
    "TimingProvenance",
    "TrajectoryFollow",
    "TrajectoryPage",
    "TrajectoryRecord",
    "TrajectoryStateStore",
    "Usage",
    "WireDecodeError",
    "clip_utf8",
]
