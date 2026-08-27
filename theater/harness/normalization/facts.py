"""Shared trajectory fact builders."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from theater.harness.contracts.events import EventPath
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.normalization.values import trajectory_detail
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryUsage


class _IdentifierCallable(Protocol):
    def __call__(self, value: object) -> str | None: ...


def lane_for_kind(kind: TrajectoryKind) -> TrajectoryLane:
    """Map a TrajectoryKind to its canonical TrajectoryLane.

    ERROR routes to THEATER (the daemon's version), not MODEL.
    """
    if kind is TrajectoryKind.USER:
        return TrajectoryLane.INPUT
    if kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        return TrajectoryLane.TOOLS
    if kind is TrajectoryKind.ERROR:
        return TrajectoryLane.THEATER
    return TrajectoryLane.MODEL


def tool_failure(status: TrajectoryStatus, detail: str) -> TrajectoryFailure | None:
    """Build a TOOL-category failure when status is ERROR."""
    if status is not TrajectoryStatus.ERROR:
        return None
    return TrajectoryFailure(TrajectoryFailureCategory.TOOL, detail=detail)


def path_details(paths: Sequence[EventPath]) -> tuple[DetailField, ...]:
    """Build path.mode detail fields with safe_trajectory_text sanitization."""
    return tuple(
        trajectory_detail(f"path.{path.mode}", path.path, format=ContentFormat.PATH)
        for path in paths
    )


def fact_builder(
    *,
    source: str,
    identifier: _IdentifierCallable,
    lane: Callable[[TrajectoryKind], TrajectoryLane] = lane_for_kind,
) -> Callable[..., TrajectoryFact]:
    """Return a TrajectoryFact constructor with shared correlation-id clamping.

    The returned callable clamps raw_index and event_ordinal to >= 0, runs
    all seven correlation ids through *identifier*, and tuple()s the details.
    """

    def build(
        *,
        kind: TrajectoryKind,
        summary: str,
        status: TrajectoryStatus = TrajectoryStatus.UNKNOWN,
        lane_override: TrajectoryLane | None = None,
        native_id: str | None = None,
        fallback_id: str | None = None,
        revision: int = 0,
        raw_index: int = 0,
        event_ordinal: int = 0,
        turn_id: str | None = None,
        step_id: str | None = None,
        request_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
        mcp_server: str | None = None,
        mcp_tool: str | None = None,
        timing: Timing | None = None,
        usage: TrajectoryUsage | None = None,
        failure: TrajectoryFailure | None = None,
        details: Sequence[DetailField] = (),
    ) -> TrajectoryFact:
        native = identifier(native_id)
        if native is None:
            native = identifier(fallback_id)
        resolved_lane = lane_override if lane_override is not None else lane(kind)
        return TrajectoryFact(
            kind=kind,
            lane=resolved_lane,
            source=source,
            summary=summary,
            status=status,
            native_id=native,
            revision=max(0, revision),
            raw_index=max(0, raw_index),
            event_ordinal=max(0, event_ordinal),
            turn_id=identifier(turn_id),
            step_id=identifier(step_id),
            request_id=identifier(request_id),
            call_id=identifier(call_id),
            parent_call_id=identifier(parent_call_id),
            mcp_server=identifier(mcp_server),
            mcp_tool=identifier(mcp_tool),
            timing=timing,
            usage=usage,
            failure=failure,
            details=tuple(details),
        )

    return build
