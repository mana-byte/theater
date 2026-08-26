"""Selected trajectory hierarchy and provenance ribbon."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from theater.constants.regie_trajectory import TRAJECTORY_BREADCRUMB_HEIGHT
from theater.regie.trajectory.render.badges import provenance_badges
from theater.regie.trajectory.render.records import sanitize_text
from theater.trajectory import TrajectoryRecord, TrajectoryRequest, TrajectoryToolOperation


def _short(value: str, limit: int = 28) -> str:
    clean = sanitize_text(value).replace("\r", " ").replace("\n", " ")
    return clean if len(clean) <= limit else f"{clean[: limit - 1]}…"


def breadcrumb_text(
    record: TrajectoryRecord | None,
    *,
    request: TrajectoryRequest | None = None,
    tool: TrajectoryToolOperation | None = None,
) -> Text:
    """Build the selected turn, request, tool, and event path."""
    line = Text(no_wrap=True, overflow="ellipsis")
    if record is None:
        line.append("No selected activity", style="dim")
        return line
    turn_id = request.turn_id if request is not None else record.turn_id
    if turn_id:
        line.append("TURN ", style="bold dim")
        line.append(_short(turn_id), style="dim")
        line.append("  ›  ", style="dim")
    if request is not None:
        model = request.model or "model unknown"
        if request.provider and not model.startswith(f"{request.provider}/"):
            model = f"{request.provider}/{model}"
        line.append("REQUEST ", style="bold dim")
        line.append(_short(model), style="cyan dim")
        line.append("  ›  ", style="dim")
    if tool is not None:
        line.append("TOOL ", style="bold dim")
        line.append(_short(tool.tool_name or "unknown tool"), style="yellow dim")
        line.append("  ›  ", style="dim")
    line.append(record.kind.value.replace("_", " ").upper(), style="bold dim")
    if record.summary:
        line.append(f"  {_short(record.summary, 54)}", style="dim")
    return line


class TrajectoryBreadcrumb(Vertical):
    """Persistent two-line selection context."""

    can_focus = False

    DEFAULT_CSS = f"""
    TrajectoryBreadcrumb {{
        width: 1fr;
        min-width: 0;
        height: {TRAJECTORY_BREADCRUMB_HEIGHT};
        min-height: {TRAJECTORY_BREADCRUMB_HEIGHT};
        padding: 0 1;
        border-bottom: solid $foreground 12%;
        background: $foreground 2%;
    }}
    TrajectoryBreadcrumb Static {{
        width: 1fr;
        min-width: 0;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._key: tuple[object, ...] | None = None

    def compose(self) -> ComposeResult:
        yield Static("No selected activity", id="trajectory-breadcrumb-path", markup=False)
        yield Static("", id="trajectory-breadcrumb-badges", markup=False)

    def update_context(
        self,
        record: TrajectoryRecord | None,
        *,
        request: TrajectoryRequest | None = None,
        tool: TrajectoryToolOperation | None = None,
    ) -> None:
        key = (record, request, tool)
        if key == self._key:
            return
        self._key = key
        self.query_one("#trajectory-breadcrumb-path", Static).update(
            breadcrumb_text(record, request=request, tool=tool)
        )
        self.query_one("#trajectory-breadcrumb-badges", Static).update(
            provenance_badges(record, request=request, tool=tool)
        )


__all__ = ["TrajectoryBreadcrumb", "breadcrumb_text"]
