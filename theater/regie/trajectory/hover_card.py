"""Stable timeline hover detail independent of Textual's native tooltip lifecycle."""

from __future__ import annotations

from textual.geometry import Offset
from textual.widgets import Static

from theater.constants.regie_trajectory import TIMELINE_HOVER_CARD_MAX_WIDTH
from theater.regie.trajectory.render import tooltip_text
from theater.trajectory import Timing, TrajectoryRecord


class TimelineHoverCard(Static):
    """Show bounded record detail without consuming trajectory layout space."""

    DEFAULT_CSS = f"""
    TimelineHoverCard {{
        position: absolute;
        overlay: screen;
        layer: trajectory-overlay;
        display: none;
        width: auto;
        max-width: {TIMELINE_HOVER_CARD_MAX_WIDTH};
        height: auto;
        padding: 0 1;
        border: solid $foreground 20%;
        background: $panel;
        color: $text;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        offset-x: -50%;
        offset-y: -100%;
        constrain: inside inflect;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=False, **kwargs)
        self._rendered_key: tuple[str, int, Timing | None, str | None] | None = None
        self._visible_record_id: str | None = None
        self._viewport_width = TIMELINE_HOVER_CARD_MAX_WIDTH

    @property
    def record_id(self) -> str | None:
        return self._visible_record_id

    def fit_to_viewport(self, width: int) -> None:
        max_width = max(1, min(TIMELINE_HOVER_CARD_MAX_WIDTH, width))
        if max_width == self._viewport_width:
            return
        self._viewport_width = max_width
        self.styles.max_width = max_width

    def show_record(
        self,
        record: TrajectoryRecord,
        anchor: Offset,
        *,
        timing: Timing | None = None,
        timing_scope: str | None = None,
    ) -> None:
        key = (record.record_id, record.revision, timing, timing_scope)
        content_changed = key != self._rendered_key
        position_changed = anchor != self.absolute_offset
        if content_changed:
            self.update(tooltip_text(record, timing=timing, timing_scope=timing_scope))
            self._rendered_key = key
        self._visible_record_id = record.record_id
        self.absolute_offset = anchor
        if not self.display:
            self.display = True
        elif position_changed and not content_changed:
            self.refresh(layout=True)

    def hide(self) -> None:
        self._visible_record_id = None
        if self.display:
            self.display = False


__all__ = ["TimelineHoverCard"]
