"""Canonical trajectory records and record payload values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_MAX_LINKS_PER_RECORD,
    TRAJECTORY_SOURCE_MAX_BYTES,
)
from theater.trajectory.content import (
    ContentPreview,
    DetailField,
    bound_detail_fields,
    bounded_text,
)
from theater.trajectory.enums import (
    CostProvenance,
    LinkDirection,
    TimingProvenance,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.validation import (
    enum_value,
    integer,
    keys,
    mapping,
    number_or_none,
    sequence,
    string,
    string_or_none,
)


@dataclass(frozen=True, slots=True)
class Timing:
    start: float | None = None
    end: float | None = None
    duration_ms: float | None = None
    provenance: TimingProvenance = TimingProvenance.UNAVAILABLE
    first_token: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("start", self.start),
            ("end", self.end),
            ("first_token", self.first_token),
            ("duration_ms", self.duration_ms),
        ):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise TrajectoryValidationError(f"timing.{name} must be a finite number or null")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise TrajectoryValidationError("timing.duration_ms must be non-negative")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise TrajectoryValidationError("timing.end must not precede timing.start")
        if (
            self.first_token is not None
            and self.start is not None
            and self.first_token < self.start
        ):
            raise TrajectoryValidationError("timing.first_token must not precede timing.start")
        if self.first_token is not None and self.end is not None and self.first_token > self.end:
            raise TrajectoryValidationError("timing.first_token must not follow timing.end")
        object.__setattr__(
            self, "provenance", enum_value(TimingProvenance, self.provenance, "timing.provenance")
        )

    def to_wire(self) -> dict[str, object]:
        value: dict[str, object] = {
            "start": self.start,
            "end": self.end,
            "duration_ms": self.duration_ms,
            "provenance": self.provenance.value,
        }
        if self.first_token is not None:
            value["first_token"] = self.first_token
        return value

    @property
    def ttft_ms(self) -> float | None:
        if self.start is None or self.first_token is None:
            return None
        return max(0.0, (self.first_token - self.start) * 1000)

    @property
    def generation_duration_ms(self) -> float | None:
        if self.first_token is None or self.end is None:
            return None
        return max(0.0, (self.end - self.first_token) * 1000)

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "timing")
        keys(
            data,
            required=set(),
            optional={"start", "end", "first_token", "duration_ms", "provenance"},
            label="timing",
        )
        return cls(
            start=number_or_none(data.get("start"), "timing.start"),
            end=number_or_none(data.get("end"), "timing.end"),
            first_token=number_or_none(data.get("first_token"), "timing.first_token"),
            duration_ms=number_or_none(data.get("duration_ms"), "timing.duration_ms"),
            provenance=enum_value(
                TimingProvenance,
                data.get("provenance", TimingProvenance.UNAVAILABLE.value),
                "timing.provenance",
            ),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryUsage:
    model: str | None = None
    provider: str | None = None
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    cost_provenance: CostProvenance = CostProvenance.UNKNOWN

    def __post_init__(self) -> None:
        for name in ("model", "provider", "request_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    bounded_text(
                        value,
                        max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                        label=f"usage.{name}",
                        nonempty=True,
                    ),
                )
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
        object.__setattr__(
            self,
            "cost_provenance",
            enum_value(CostProvenance, self.cost_provenance, "usage.cost_provenance"),
        )
        if self.cost_usd is None and self.cost_provenance is not CostProvenance.UNKNOWN:
            raise TrajectoryValidationError("usage.cost provenance requires a cost")

    def to_wire(self) -> dict[str, object]:
        value: dict[str, object] = {
            "model": self.model,
            "provider": self.provider,
            "request_id": self.request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
        }
        if self.cost_provenance is not CostProvenance.UNKNOWN:
            value["cost_provenance"] = self.cost_provenance.value
        return value

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "usage")
        required = {
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        }
        keys(
            data,
            required=required,
            optional={"model", "provider", "request_id", "cost_usd", "cost_provenance"},
            label="usage",
        )
        return cls(
            model=string_or_none(data.get("model"), "usage.model"),
            provider=string_or_none(data.get("provider"), "usage.provider"),
            request_id=string_or_none(data.get("request_id"), "usage.request_id"),
            input_tokens=integer(data["input_tokens"], "usage.input_tokens"),
            output_tokens=integer(data["output_tokens"], "usage.output_tokens"),
            reasoning_tokens=integer(data["reasoning_tokens"], "usage.reasoning_tokens"),
            cache_read_tokens=integer(data["cache_read_tokens"], "usage.cache_read_tokens"),
            cache_write_tokens=integer(data["cache_write_tokens"], "usage.cache_write_tokens"),
            cost_usd=number_or_none(data.get("cost_usd"), "usage.cost_usd"),
            cost_provenance=enum_value(
                CostProvenance,
                data.get("cost_provenance", CostProvenance.UNKNOWN.value),
                "usage.cost_provenance",
            ),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryFailure:
    category: TrajectoryFailureCategory = TrajectoryFailureCategory.UNKNOWN
    code: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "category",
            enum_value(TrajectoryFailureCategory, self.category, "failure.category"),
        )
        if self.code is not None:
            object.__setattr__(
                self,
                "code",
                bounded_text(
                    self.code,
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label="failure.code",
                    nonempty=True,
                ),
            )
        object.__setattr__(
            self,
            "detail",
            ContentPreview.from_text(self.detail, max_bytes=TRAJECTORY_SOURCE_MAX_BYTES).text,
        )

    def to_wire(self) -> dict[str, object]:
        return {"category": self.category.value, "code": self.code, "detail": self.detail}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory failure")
        keys(data, required={"category"}, optional={"code", "detail"}, label="trajectory failure")
        return cls(
            category=enum_value(TrajectoryFailureCategory, data["category"], "failure.category"),
            code=string_or_none(data.get("code"), "failure.code"),
            detail=string(data.get("detail", ""), "failure.detail"),
        )


@dataclass(frozen=True, slots=True)
class ParticipantLink:
    participant_id: str
    relation: str
    direction: LinkDirection = LinkDirection.RELATED
    target_record_id: str | None = None
    correlation_type: str | None = None
    correlation_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "participant_id",
            bounded_text(
                self.participant_id,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="participant link id",
                nonempty=True,
            ),
        )
        object.__setattr__(
            self,
            "relation",
            bounded_text(
                self.relation,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="participant link relation",
                nonempty=True,
            ),
        )
        object.__setattr__(
            self, "direction", enum_value(LinkDirection, self.direction, "link.direction")
        )
        if self.target_record_id is not None:
            object.__setattr__(
                self,
                "target_record_id",
                bounded_text(
                    self.target_record_id,
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label="participant link target record id",
                    nonempty=True,
                ),
            )
        if (self.correlation_type is None) != (self.correlation_key is None):
            raise TrajectoryValidationError(
                "participant link correlation type and key must be provided together"
            )
        if self.correlation_type is not None:
            assert self.correlation_key is not None
            correlation_type = bounded_text(
                self.correlation_type,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="participant link correlation type",
                nonempty=True,
            )
            if correlation_type not in {"bus_row", "job_handle", "session_id", "spawn_id"}:
                raise TrajectoryValidationError(
                    "participant link correlation type must be bus_row, job_handle, session_id, or "
                    "spawn_id"
                )
            object.__setattr__(self, "correlation_type", correlation_type)
            object.__setattr__(
                self,
                "correlation_key",
                bounded_text(
                    self.correlation_key,
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label="participant link correlation key",
                    nonempty=True,
                ),
            )

    def to_wire(self) -> dict[str, object]:
        value: dict[str, object] = {
            "participant_id": self.participant_id,
            "relation": self.relation,
            "direction": self.direction.value,
        }
        if self.target_record_id is not None:
            value["target_record_id"] = self.target_record_id
        if self.correlation_type is not None:
            value["correlation_type"] = self.correlation_type
            value["correlation_key"] = self.correlation_key
        return value

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "participant link")
        keys(
            data,
            required={"participant_id", "relation"},
            optional={"direction", "target_record_id", "correlation_type", "correlation_key"},
            label="participant link",
        )
        return cls(
            participant_id=string(data["participant_id"], "link.participant_id"),
            relation=string(data["relation"], "link.relation"),
            direction=enum_value(
                LinkDirection,
                data.get("direction", LinkDirection.RELATED.value),
                "link.direction",
            ),
            target_record_id=string_or_none(data.get("target_record_id"), "link.target_record_id"),
            correlation_type=string_or_none(data.get("correlation_type"), "link.correlation_type"),
            correlation_key=string_or_none(data.get("correlation_key"), "link.correlation_key"),
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
    request_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    mcp_server: str | None = None
    mcp_tool: str | None = None
    links: tuple[ParticipantLink, ...] = ()
    timing: Timing | None = None
    usage: TrajectoryUsage | None = None
    failure: TrajectoryFailure | None = None
    retry_of_record_id: str | None = None
    retry_attempt: int | None = None
    details: tuple[DetailField, ...] = ()
    source_offset: int | None = None

    def __post_init__(self) -> None:  # noqa: PLR0912
        for name in ("record_id", "participant_id", "source_epoch"):
            object.__setattr__(
                self,
                name,
                bounded_text(
                    getattr(self, name),
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label=f"record.{name}",
                    nonempty=True,
                ),
            )
        object.__setattr__(
            self,
            "source",
            bounded_text(
                self.source,
                max_bytes=TRAJECTORY_SOURCE_MAX_BYTES,
                label="record.source",
                nonempty=True,
            ),
        )
        if type(self.revision) is not int or self.revision < 0:
            raise TrajectoryValidationError("record.revision must be a non-negative integer")
        for name in ("raw_index", "event_ordinal"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TrajectoryValidationError(f"record.{name} must be a non-negative integer")
        if self.source_offset is not None and (
            type(self.source_offset) is not int or self.source_offset < 0
        ):
            raise TrajectoryValidationError(
                "record.source_offset must be a non-negative integer or null"
            )
        object.__setattr__(self, "lane", enum_value(TrajectoryLane, self.lane, "record.lane"))
        object.__setattr__(self, "kind", enum_value(TrajectoryKind, self.kind, "record.kind"))
        object.__setattr__(
            self, "status", enum_value(TrajectoryStatus, self.status, "record.status")
        )
        object.__setattr__(self, "summary", ContentPreview.from_text(self.summary).text)
        for name in (
            "native_id",
            "turn_id",
            "step_id",
            "request_id",
            "call_id",
            "parent_call_id",
            "mcp_server",
            "mcp_tool",
            "retry_of_record_id",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    bounded_text(
                        value,
                        max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                        label=f"record.{name}",
                        nonempty=True,
                    ),
                )
        if (self.mcp_server is None) != (self.mcp_tool is None):
            raise TrajectoryValidationError("record MCP identity requires both server and tool")
        object.__setattr__(self, "links", tuple(self.links))
        if len(self.links) > TRAJECTORY_MAX_LINKS_PER_RECORD:
            raise TrajectoryValidationError(
                f"record.links exceeds {TRAJECTORY_MAX_LINKS_PER_RECORD} values"
            )
        if any(not isinstance(link, ParticipantLink) for link in self.links):
            raise TrajectoryValidationError("record.links must contain ParticipantLink values")
        if self.timing is not None and not isinstance(self.timing, Timing):
            raise TrajectoryValidationError("record.timing must be Timing or null")
        if self.usage is not None and not isinstance(self.usage, TrajectoryUsage):
            raise TrajectoryValidationError("record.usage must be TrajectoryUsage or null")
        if self.failure is not None and not isinstance(self.failure, TrajectoryFailure):
            raise TrajectoryValidationError("record.failure must be TrajectoryFailure or null")
        if self.retry_attempt is not None and self.retry_of_record_id is None:
            raise TrajectoryValidationError("record retry attempt requires a retry link")
        if self.retry_attempt is not None and (
            type(self.retry_attempt) is not int or self.retry_attempt <= 0
        ):
            raise TrajectoryValidationError("record.retry_attempt must be a positive integer")
        object.__setattr__(self, "details", bound_detail_fields(self.details))

    def to_wire(self) -> dict[str, object]:
        value = {
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
            "source_offset": self.source_offset,
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
        if self.request_id is not None:
            value["request_id"] = self.request_id
        if self.mcp_server is not None:
            value["mcp_server"] = self.mcp_server
            value["mcp_tool"] = self.mcp_tool
        if self.failure is not None:
            value["failure"] = self.failure.to_wire()
        if self.retry_of_record_id is not None:
            value["retry_of_record_id"] = self.retry_of_record_id
            value["retry_attempt"] = self.retry_attempt
        return value

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory record")
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
            "source_offset",
            "event_ordinal",
            "turn_id",
            "step_id",
            "request_id",
            "call_id",
            "parent_call_id",
            "mcp_server",
            "mcp_tool",
            "links",
            "timing",
            "usage",
            "failure",
            "retry_of_record_id",
            "retry_attempt",
            "details",
        }
        keys(data, required=required, optional=optional, label="trajectory record")
        return cls(
            record_id=string(data["record_id"], "record.record_id"),
            revision=integer(data["revision"], "record.revision"),
            participant_id=string(data["participant_id"], "record.participant_id"),
            source_epoch=string(data["source_epoch"], "record.source_epoch"),
            lane=enum_value(TrajectoryLane, data["lane"], "record.lane"),
            kind=enum_value(TrajectoryKind, data["kind"], "record.kind"),
            source=string(data["source"], "record.source"),
            summary=string(data["summary"], "record.summary"),
            status=enum_value(TrajectoryStatus, data["status"], "record.status"),
            native_id=string_or_none(data.get("native_id"), "record.native_id"),
            raw_index=integer(data.get("raw_index", 0), "record.raw_index"),
            source_offset=(
                integer(data["source_offset"], "record.source_offset")
                if data.get("source_offset") is not None
                else None
            ),
            event_ordinal=integer(data.get("event_ordinal", 0), "record.event_ordinal"),
            turn_id=string_or_none(data.get("turn_id"), "record.turn_id"),
            step_id=string_or_none(data.get("step_id"), "record.step_id"),
            request_id=string_or_none(data.get("request_id"), "record.request_id"),
            call_id=string_or_none(data.get("call_id"), "record.call_id"),
            parent_call_id=string_or_none(data.get("parent_call_id"), "record.parent_call_id"),
            mcp_server=string_or_none(data.get("mcp_server"), "record.mcp_server"),
            mcp_tool=string_or_none(data.get("mcp_tool"), "record.mcp_tool"),
            links=tuple(
                ParticipantLink.from_wire(item)
                for item in sequence(data.get("links", []), "record.links")
            ),
            timing=Timing.from_wire(data["timing"]) if data.get("timing") is not None else None,
            usage=TrajectoryUsage.from_wire(data["usage"])
            if data.get("usage") is not None
            else None,
            failure=TrajectoryFailure.from_wire(data["failure"])
            if data.get("failure") is not None
            else None,
            retry_of_record_id=string_or_none(
                data.get("retry_of_record_id"), "record.retry_of_record_id"
            ),
            retry_attempt=(
                integer(data["retry_attempt"], "record.retry_attempt")
                if data.get("retry_attempt") is not None
                else None
            ),
            details=tuple(
                DetailField.from_wire(item)
                for item in sequence(data.get("details", []), "record.details")
            ),
        )


__all__ = ["ParticipantLink", "Timing", "TrajectoryFailure", "TrajectoryRecord", "TrajectoryUsage"]
