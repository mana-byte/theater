"""Canonical classification for MCP operations observed in harness transcripts."""

from __future__ import annotations

import json
from dataclasses import replace

from theater.constants.harness import HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME
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
    if fact.mcp_server == HARNESS_MCP_WAIT_SERVER_NAME:
        return _with_refusal(fact)
    if fact.mcp_server != HARNESS_MCP_SERVER_NAME:
        return fact
    fact = _with_refusal(fact)
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


def _with_refusal(fact: TrajectoryFact) -> TrajectoryFact:
    """A Theater refusal delivered as ordinary text ({"ok": false, ...}) is still an error."""
    if fact.kind is not TrajectoryKind.TOOL_RESULT or fact.status is TrajectoryStatus.ERROR:
        return fact
    code = _refusal_code(fact)
    if code is None:
        return fact
    return replace(
        fact,
        status=TrajectoryStatus.ERROR,
        failure=TrajectoryFailure(TrajectoryFailureCategory.TOOL, code=code),
    )


def _refusal_code(fact: TrajectoryFact) -> str | None:
    texts = [
        d.value if isinstance(d.value, str) else d.value.text
        for d in fact.details
        if d.name == "result"
    ] or [fact.summary]
    for text in texts:
        candidates = [line for line in text.splitlines() if line.startswith('{"ok":false')]
        if text.lstrip().startswith("{"):
            candidates.insert(0, text)
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                error = parsed.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                return code if isinstance(code, str) else "error"
    return None


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
