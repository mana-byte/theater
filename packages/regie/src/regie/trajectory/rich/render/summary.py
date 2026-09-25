"""One-line trajectory status and totals for the view header."""

from __future__ import annotations

import time

from rich.text import Text

from regie.trajectory.rich.render.records import compact_cost, compact_number, sanitize_text
from regie.trajectory.ui_constants import (
    KIND_GLYPHS_BY_VALUE,
    TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND,
    TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR,
    TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE,
)
from theater.frontend.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryCurrentOperation,
    TrajectoryOverview,
    TrajectoryParticipantState,
)

_BLOCKED = frozenset({PanelState.STALE, PanelState.UNAVAILABLE, PanelState.UNTRUSTED})


def summary_line(
    panel: PanelStateInfo,
    overview: TrajectoryOverview,
    *,
    loading: bool,
    stale_message: str = "",
) -> Text:
    """Status on the left, then tokens, cost, and active time."""
    status, style = _status(panel, overview, loading=loading, stale_message=stale_message)
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(" ● ", style=style)
    line.append(status, style=f"bold {style}".strip())
    for part in _totals(overview):
        line.append("   ·   ", style="dim")
        line.append(part, style="dim")
    return line


def duration_text(duration_ms: float | None, start: float | None = None) -> str:
    if duration_ms is None:
        if start is None or start > time.time():
            return ""
        duration_ms = (time.time() - start) * TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND
    if duration_ms < TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND:
        return f"{duration_ms:g}ms"
    seconds = duration_ms / TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND
    if seconds < TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE)
    if minutes < TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR:
        return f"{minutes}m {remainder}s"
    hours, minutes = divmod(minutes, TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR)
    return f"{hours}h {minutes}m"


def _status(
    panel: PanelStateInfo,
    overview: TrajectoryOverview,
    *,
    loading: bool,
    stale_message: str,
) -> tuple[str, str]:
    current = overview.current
    if panel.state in _BLOCKED:
        message = _one_line(panel.message or stale_message)
        return " · ".join((panel.state.value.title(), *([message] if message else []))), "red"
    if panel.participant_state is TrajectoryParticipantState.DEAD:
        return "Dead", "red"
    if panel.participant_state is TrajectoryParticipantState.EXTERNAL:
        return "External · live updates unavailable", "yellow"
    if loading and current is None:
        return "Loading trajectory…", "dim"
    if panel.state is PanelState.WAITING:
        return "Waiting", "yellow"
    if current is not None and panel.participant_state is TrajectoryParticipantState.LIVE:
        return _current_text(current), "cyan"
    return "Idle", "green"


def _current_text(current: TrajectoryCurrentOperation) -> str:
    kind = current.kind.value.replace("_", " ")
    pieces = [
        f"{KIND_GLYPHS_BY_VALUE.get(current.kind.value, '?')} "
        f"{current.status.value.replace('_', ' ').title()} {kind}"
    ]
    if duration := duration_text(current.duration_ms, current.start):
        pieces.append(duration)
    if current.summary:
        pieces.append(_one_line(current.summary))
    return " · ".join(pieces)


def _totals(overview: TrajectoryOverview) -> list[str]:
    parts = [
        f"{compact_number(overview.model_operations)} model",
        f"{compact_number(overview.tool_operations)} tools",
    ]
    tokens = (
        overview.input_tokens
        + overview.output_tokens
        + overview.cache_read_tokens
        + overview.cache_write_tokens
    )
    if tokens:
        parts.append(f"{compact_number(tokens)} tok")
    cost = overview.reported_cost_usd
    if cost is None:
        cost = overview.estimated_cost_usd
    if cost is not None:
        parts.append(f"${compact_cost(cost)}")
    if overview.active_duration_ms is not None:
        parts.append(duration_text(overview.active_duration_ms))
    return parts


def _one_line(value: str) -> str:
    return sanitize_text(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")


__all__ = ["duration_text", "summary_line"]
