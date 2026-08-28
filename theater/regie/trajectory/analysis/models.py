"""Immutable values produced by trajectory analysis."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from theater.trajectory import (
    LinkDirection,
    ParticipantLink,
    Timing,
    TrajectoryFailure,
    TrajectoryStatus,
)


@dataclass(frozen=True, slots=True)
class WaterfallRow:
    key: str
    label: str
    record_id: str
    member_record_ids: tuple[str, ...]
    timing: Timing | None
    status: TrajectoryStatus
    depth: int = 0
    scope: bool = False


@dataclass(frozen=True, slots=True)
class WaterfallProjection:
    scope_id: str
    turn_id: str | None
    label: str
    record_ids: tuple[str, ...]
    rows: tuple[WaterfallRow, ...]
    start: float | None = None
    end: float | None = None
    first_token: float | None = None


@dataclass(frozen=True, slots=True)
class FileOperationActivity:
    operation_id: str
    record_id: str
    record_ids: tuple[str, ...]
    modes: frozenset[str]
    tool_name: str | None
    status: TrajectoryStatus
    timing: Timing | None


@dataclass(frozen=True, slots=True)
class FileActivity:
    path: str
    modes: frozenset[str]
    record_ids: tuple[str, ...]
    status: TrajectoryStatus
    operations: tuple[FileOperationActivity, ...]

    @property
    def operation_count(self) -> int:
        return len(self.operations)


@dataclass(frozen=True, slots=True)
class DelegationActivity:
    participant_id: str
    directions: frozenset[LinkDirection]
    relations: tuple[str, ...]
    event_count: int
    record_ids: tuple[str, ...]
    latest_record_id: str
    latest_summary: str
    latest_status: TrajectoryStatus
    target: ParticipantLink


@dataclass(frozen=True, slots=True)
class ResourceValues:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_tokens: int = 0
    cost_usd: float | None = None
    cost_provenance: str = "unknown"
    cost_complete: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens + self.cache_tokens


@dataclass(frozen=True, slots=True)
class ResourceActivity:
    key: str
    scope: str
    label: str
    model: str | None
    values: ResourceValues
    record_ids: tuple[str, ...]
    record_id: str
    depth: int = 0


@dataclass(frozen=True, slots=True)
class ProblemActivity:
    record_id: str
    member_record_ids: tuple[str, ...]
    label: str
    status: TrajectoryStatus
    failure: TrajectoryFailure | None
    retry_of_record_id: str | None
    retry_attempt: int | None
    chain_depth: int


@dataclass(frozen=True, slots=True)
class TrajectoryAnalysisIndex:
    waterfalls: tuple[WaterfallProjection, ...] = ()
    waterfall_by_scope: Mapping[str, WaterfallProjection] = MappingProxyType({})
    waterfall_scope_by_record: Mapping[str, str] = MappingProxyType({})
    files: tuple[FileActivity, ...] = ()
    delegations: tuple[DelegationActivity, ...] = ()
    resources: tuple[ResourceActivity, ...] = ()
    problems: tuple[ProblemActivity, ...] = ()

    def waterfall_for(
        self,
        record_id: str | None,
        visible_ids: frozenset[str] | set[str] | None = None,
    ) -> WaterfallProjection | None:
        visible = None if visible_ids is None else frozenset(visible_ids)
        scope_id = self.waterfall_scope_by_record.get(record_id or "")
        candidate = self.waterfall_by_scope.get(scope_id or "")
        if candidate is not None and (
            visible is None or visible.intersection(candidate.record_ids)
        ):
            return candidate
        for fallback in reversed(self.waterfalls):
            if visible is None or visible.intersection(fallback.record_ids):
                return fallback
        return None


__all__ = [
    "DelegationActivity",
    "FileActivity",
    "FileOperationActivity",
    "ProblemActivity",
    "ResourceActivity",
    "ResourceValues",
    "TrajectoryAnalysisIndex",
    "WaterfallProjection",
    "WaterfallRow",
]
