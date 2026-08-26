"""Canonical classification for MCP operations observed in harness transcripts."""

from __future__ import annotations

from dataclasses import replace

from theater.constants.harness import HARNESS_MCP_SERVER_NAME
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory import (
    TrajectoryFailure,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)


def classify_mcp_fact(fact: TrajectoryFact) -> TrajectoryFact:
    """Move calls to Theater's MCP server out of the generic tool domain."""
    if fact.mcp_server != HARNESS_MCP_SERVER_NAME:
        return fact
    kind = {
        TrajectoryKind.TOOL_CALL: TrajectoryKind.THEATER_CALL,
        TrajectoryKind.TOOL_RESULT: TrajectoryKind.THEATER_RESULT,
    }.get(fact.kind)
    if kind is None:
        return fact
    failure = fact.failure
    if failure is not None and failure.category is TrajectoryFailureCategory.TOOL:
        failure = TrajectoryFailure(
            TrajectoryFailureCategory.THEATER,
            code=failure.code,
            detail=failure.detail,
        )
    return replace(
        fact,
        kind=kind,
        lane=TrajectoryLane.THEATER,
        summary=_summary(fact, kind),
        failure=failure,
    )


def _summary(fact: TrajectoryFact, kind: TrajectoryKind) -> str:
    tool = fact.mcp_tool or "operation"
    if kind is TrajectoryKind.THEATER_CALL:
        return tool
    if fact.status is TrajectoryStatus.ERROR:
        return f"{tool} failed"
    if fact.status in {
        TrajectoryStatus.COMPLETED,
        TrajectoryStatus.CANCELLED,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
    }:
        return f"{tool} {fact.status.value}"
    return f"{tool} result"


__all__ = ["classify_mcp_fact"]
