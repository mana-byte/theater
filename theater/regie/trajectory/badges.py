"""Compact provenance badges for trajectory context."""

from __future__ import annotations

from rich.text import Text

from theater.regie.trajectory.render import sanitize_text
from theater.trajectory import (
    CostProvenance,
    Timing,
    TimingProvenance,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryToolOperation,
    TrajectoryUsage,
)

_STATUS_STYLES = {
    TrajectoryStatus.CANCELLED: "dim yellow",
    TrajectoryStatus.ERROR: "dim red",
    TrajectoryStatus.INTERRUPTED: "dim yellow",
    TrajectoryStatus.PARTIAL: "dim yellow",
    TrajectoryStatus.PENDING: "dim yellow",
    TrajectoryStatus.RUNNING: "dim cyan",
    TrajectoryStatus.TIMEOUT: "dim red",
    TrajectoryStatus.UNKNOWN: "dim",
}


def _append_badge(line: Text, label: str, value: str, style: str = "dim") -> None:
    if line:
        line.append("  ")
    line.append(f" {label.upper()} ", style="reverse dim")
    line.append(f" {sanitize_text(value)} ", style=style)


def _timing_for(
    record: TrajectoryRecord,
    request: TrajectoryRequest | None,
    tool: TrajectoryToolOperation | None,
) -> Timing | None:
    if tool is not None and tool.timing is not None:
        return tool.timing
    if request is not None and request.timing is not None:
        return request.timing
    return record.timing


def _usage_for(
    record: TrajectoryRecord, request: TrajectoryRequest | None
) -> TrajectoryUsage | None:
    if request is not None and request.usage is not None:
        return request.usage
    return record.usage


def provenance_badges(
    record: TrajectoryRecord | None,
    *,
    request: TrajectoryRequest | None = None,
    tool: TrajectoryToolOperation | None = None,
) -> Text:
    """Render bounded source, status, timing, cost, and link fidelity."""
    line = Text(no_wrap=True, overflow="ellipsis")
    if record is None:
        _append_badge(line, "scope", "no selection")
        return line
    _append_badge(line, "source", record.source)
    status = (
        tool.status
        if tool is not None
        else request.status
        if request is not None
        else record.status
    )
    _append_badge(line, "state", status.value, _STATUS_STYLES.get(status, "dim green"))
    timing = _timing_for(record, request, tool)
    provenance = timing.provenance if timing is not None else TimingProvenance.UNAVAILABLE
    _append_badge(
        line,
        "timing",
        provenance.value,
        "dim yellow" if provenance is TimingProvenance.UNAVAILABLE else "dim cyan",
    )
    usage = _usage_for(record, request)
    cost = usage.cost_provenance if usage is not None else CostProvenance.UNKNOWN
    _append_badge(
        line,
        "cost",
        cost.value,
        "dim yellow" if cost is CostProvenance.UNKNOWN else "dim cyan",
    )
    if record.links:
        fidelity = "exact" if any(link.target_record_id for link in record.links) else "participant"
        _append_badge(line, "link", fidelity, "dim magenta")
    return line


__all__ = ["provenance_badges"]
