"""Current trajectory scope and capability summary."""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from theater.regie.trajectory.constants import (
    KIND_GLYPHS_BY_VALUE,
    TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD,
    TRAJECTORY_OVERVIEW_HEIGHT,
    TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND,
    TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR,
    TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE,
    TRAJECTORY_OVERVIEW_TICK_SECONDS,
)
from theater.regie.trajectory.render import sanitize_text
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryCapabilities,
    TrajectoryCurrentOperation,
    TrajectoryFeature,
    TrajectoryIncompleteReason,
    TrajectoryOverview,
    TrajectoryParticipantState,
)


class TrajectoryOverviewStrip(Vertical):
    """A non-interactive current-scope trajectory summary."""

    can_focus = False
    _state_key: tuple[object, ...] | None
    _primary_text: str
    _panel: PanelStateInfo | None
    _overview: TrajectoryOverview | None
    _loading: bool
    _stale_message: str

    DEFAULT_CSS = f"""
    TrajectoryOverviewStrip {{
        width: 1fr;
        min-width: 0;
        height: {TRAJECTORY_OVERVIEW_HEIGHT};
        min-height: {TRAJECTORY_OVERVIEW_HEIGHT};
        padding: 0 1;
        border-bottom: solid $foreground 12%;
        background: $foreground 3%;
    }}
    TrajectoryOverviewStrip Label {{
        width: 1fr;
        min-width: 0;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    TrajectoryOverviewStrip #trajectory-overview-current {{
        color: $text;
        text-opacity: 85%;
    }}
    TrajectoryOverviewStrip #trajectory-overview-current.-active {{
        color: $accent;
        text-opacity: 75%;
    }}
    TrajectoryOverviewStrip #trajectory-overview-current.-warning {{
        color: $warning;
        text-opacity: 75%;
    }}
    TrajectoryOverviewStrip #trajectory-overview-current.-problem {{
        color: $error;
        text-opacity: 75%;
    }}
    TrajectoryOverviewStrip #trajectory-overview-meta {{
        color: $text-muted;
        text-opacity: 75%;
    }}
    """

    def compose(self) -> ComposeResult:
        yield Label("Loading trajectory…", id="trajectory-overview-current", markup=False)
        yield Label(
            "coverage unknown · capabilities unknown",
            id="trajectory-overview-meta",
            markup=False,
        )

    def on_mount(self) -> None:
        self._state_key = None
        self._primary_text = ""
        self._panel: PanelStateInfo | None = None
        self._overview: TrajectoryOverview | None = None
        self._loading = False
        self._stale_message = ""
        self.set_interval(TRAJECTORY_OVERVIEW_TICK_SECONDS, self._tick)

    def update_state(
        self,
        *,
        panel: PanelStateInfo,
        capabilities: TrajectoryCapabilities,
        overview: TrajectoryOverview,
        loading: bool,
        stale_message: str = "",
    ) -> None:
        """Refresh from bounded backend facts without inspecting loaded records."""
        state_key = (panel, capabilities, overview, loading, stale_message)
        if state_key == self._state_key:
            return
        self._state_key = state_key
        self._panel = panel
        self._overview = overview
        self._loading = loading
        self._stale_message = stale_message
        self._update_primary(force=True)
        meta = self.query_one("#trajectory-overview-meta", Label)
        meta_text = _meta_text(capabilities, overview)
        meta.update(meta_text)
        meta.tooltip = _meta_tooltip(capabilities, overview)

    def _tick(self) -> None:
        if self._eligible_current():
            self._update_primary(force=False)

    def _eligible_current(self) -> bool:
        if self._panel is None or self._overview is None:
            return False
        current = self._overview.current
        return (
            current is not None
            and current.duration_ms is None
            and current.start is not None
            and current.start <= time.time()
            and self._panel.state is PanelState.READY
            and self._panel.participant_state is TrajectoryParticipantState.LIVE
        )

    def _update_primary(self, *, force: bool) -> None:
        if self._panel is None or self._overview is None:
            return
        primary = _primary_text(
            self._panel,
            self._overview,
            loading=self._loading,
            stale_message=self._stale_message,
        )
        if not force and primary == self._primary_text:
            return
        self._primary_text = primary
        label = self.query_one("#trajectory-overview-current", Label)
        label.update(primary)
        label.tooltip = primary
        problem = (
            self._panel.state
            in {
                PanelState.STALE,
                PanelState.UNAVAILABLE,
                PanelState.UNTRUSTED,
            }
            or self._panel.participant_state is TrajectoryParticipantState.DEAD
        )
        warning = not problem and (
            self._panel.state is PanelState.WAITING
            or self._panel.participant_state is TrajectoryParticipantState.EXTERNAL
        )
        active = (
            self._panel.state is PanelState.READY
            and self._panel.participant_state is TrajectoryParticipantState.LIVE
            and self._overview.current is not None
        )
        label.set_class(problem, "-problem")
        label.set_class(warning, "-warning")
        label.set_class(active, "-active")


def _primary_text(
    panel: PanelStateInfo,
    overview: TrajectoryOverview,
    *,
    loading: bool,
    stale_message: str,
) -> str:
    current = overview.current
    issue = _problem_text(overview, exclude=current.summary if current is not None else "")
    panel_message = _one_line(panel.message or stale_message)
    if panel.state in {PanelState.STALE, PanelState.UNAVAILABLE, PanelState.UNTRUSTED}:
        return _blocked_panel_text(panel, current, panel_message, issue)
    if panel.participant_state is TrajectoryParticipantState.DEAD:
        return _inactive_participant_text("Dead", "Dead · no active operation", current, issue)
    if panel.participant_state is TrajectoryParticipantState.EXTERNAL:
        return _inactive_participant_text(
            "External", "External · live updates unavailable", current, issue
        )
    if loading and current is None:
        return "Loading trajectory…"
    if panel.state is PanelState.WAITING:
        return _waiting_text(panel_message, issue)
    if current is not None and panel.participant_state is TrajectoryParticipantState.LIVE:
        return _live_current_text(current)
    return _append_issue("Idle · no active operation", issue)


def _blocked_panel_text(
    panel: PanelStateInfo,
    current: TrajectoryCurrentOperation | None,
    panel_message: str,
    issue: str,
) -> str:
    pieces = [panel.state.value.title(), *([panel_message] if panel_message else [])]
    if current is not None and current.summary:
        pieces.append(f"last incomplete: {_one_line(current.summary)}")
    if issue:
        pieces.append(f"last issue: {issue}")
    return " · ".join(pieces)


def _inactive_participant_text(
    label: str,
    fallback: str,
    current: TrajectoryCurrentOperation | None,
    issue: str,
) -> str:
    base = (
        f"{label} · last incomplete: {_one_line(current.summary)}"
        if current and current.summary
        else fallback
    )
    return _append_issue(base, issue)


def _waiting_text(panel_message: str, issue: str) -> str:
    return _append_issue("Waiting" + (f" · {panel_message}" if panel_message else ""), issue)


def _live_current_text(current: TrajectoryCurrentOperation) -> str:
    kind = current.kind
    status = current.status
    pieces = [
        f"{KIND_GLYPHS_BY_VALUE.get(kind.value, '?')} "
        f"{status.value.replace('_', ' ').title()} {kind.value.replace('_', ' ')}"
    ]
    if current.model:
        pieces.append(_one_line(current.model))
    duration = _duration_text(current.duration_ms, current.start)
    if duration:
        pieces.append(duration)
    if current.summary:
        pieces.append(_one_line(current.summary))
    return " · ".join(pieces)


def _append_issue(text: str, issue: str) -> str:
    return f"{text} · last issue: {issue}" if issue else text


def _problem_text(overview: TrajectoryOverview, *, exclude: str) -> str:
    problem = overview.latest_problem
    if problem is None or not problem.summary:
        return ""
    summary = _one_line(problem.summary)
    return "" if summary == _one_line(exclude) else summary


def _duration_text(duration_ms: float | None, start: float | None) -> str:
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


def _meta_text(capabilities: TrajectoryCapabilities, overview: TrajectoryOverview) -> str:
    parts = [
        f"{_compact_number(overview.record_count)} cached records",
        f"{_compact_number(overview.model_operations)} model ops",
        f"{_compact_number(overview.tool_operations)} tool calls",
    ]
    token_parts = (
        ("in", overview.input_tokens),
        ("out", overview.output_tokens),
        ("cache", overview.cache_read_tokens + overview.cache_write_tokens),
        ("reasoning", overview.reasoning_tokens),
    )
    parts.extend(f"{name} {_compact_number(value)} tok" for name, value in token_parts if value)
    if overview.reported_cost_usd is not None:
        parts.append(f"reported ${_compact_cost(overview.reported_cost_usd)}")
    if overview.totals_saturated:
        parts.append("totals capped")
    parts.append(_coverage_text(overview))
    if not capabilities.supported and not capabilities.unsupported:
        parts.append("capabilities unknown")
    else:
        parts.append(
            f"{len(capabilities.supported)} supported · {len(capabilities.observed)} observed"
        )
        if capabilities.unsupported:
            parts.append(f"{len(capabilities.unsupported)} unsupported")
    return " · ".join(parts)


def _meta_tooltip(capabilities: TrajectoryCapabilities, overview: TrajectoryOverview) -> str:
    coverage = ", ".join(reason.value for reason in overview.incomplete_reasons) or "complete"
    groups = (
        ("Supported", capabilities.supported),
        ("Unsupported", capabilities.unsupported),
        ("Observed", capabilities.observed),
        (
            "Unknown",
            frozenset(
                feature
                for feature in TrajectoryFeature
                if feature not in capabilities.supported and feature not in capabilities.unsupported
            ),
        ),
    )
    lines = [f"Coverage: {coverage}"]
    lines.extend(f"{label}: {_feature_names(values)}" for label, values in groups)
    return "\n".join(lines)


def _feature_names(values: frozenset[TrajectoryFeature]) -> str:
    return ", ".join(feature.value for feature in TrajectoryFeature if feature in values) or "none"


def _coverage_text(overview: TrajectoryOverview) -> str:
    if overview.scope_complete:
        return "coverage complete"
    if TrajectoryIncompleteReason.UNKNOWN in overview.incomplete_reasons:
        return "coverage unknown"
    labels = {
        TrajectoryIncompleteReason.OLDER_HISTORY: "older history",
        TrajectoryIncompleteReason.COVERAGE_GAPS: "gaps",
        TrajectoryIncompleteReason.CACHE_EVICTED: "cache eviction",
    }
    reasons = [
        labels[reason]
        for reason in TrajectoryIncompleteReason
        if reason in overview.incomplete_reasons and reason in labels
    ]
    return f"partial: {', '.join(reasons)}" if reasons else "coverage unknown"


def _compact_number(value: int) -> str:
    if value < TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD:
        return str(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            return f"{value / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def _compact_cost(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _one_line(value: str) -> str:
    return sanitize_text(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")


__all__ = ["TrajectoryOverviewStrip"]
