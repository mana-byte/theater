"""Interactive three-row participant leaf."""

from __future__ import annotations

from typing import ClassVar, Literal

from rich.cells import cell_len
from textual import events
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static

from regie.animations.marquee import clip_cells, marquee_cells, overflows_cells
from regie.animations.routes import LeafOverlay
from regie.animations.spinner import advance_spinner_frame
from regie.formatting import tilde
from regie.render.glyphs import node_label
from regie.render.layout import Key, shorten_path
from regie.ui_constants import REGIE_LEAF_MARQUEE_INTERVAL, REGIE_LEAF_SPINNER_INTERVAL

type StageMarker = Literal["tmux", "trajectory"]


def _content_changed(previous: Content, current: Content) -> bool:
    return previous.plain != current.plain or previous.spans != current.spans


class AgentLeaf(Static):
    """One participant, preserving animation state across projection refreshes."""

    ALLOW_SELECT: ClassVar[bool] = False

    DEFAULT_CSS = """
    AgentLeaf {
        height: 3;
        padding: 0 2;
        margin: 0 0;
    }
    AgentLeaf:hover {
        background: $accent 10%;
    }
    AgentLeaf.tree-staged,
    AgentLeaf.tree-trajectory-staged {
        padding: 0 2 0 0;
    }
    AgentLeaf.tree-cursor {
        background: $accent 20%;
        text-style: bold;
    }
    """

    def __init__(
        self,
        node: dict,
        prefix: str = "",
        *,
        cont_prefix: str = "",
        key: Key | None = None,
        cwd_segments: int = 2,
        participant_detail: Literal["cwd", "description"] = "cwd",
        is_first_root: bool = False,
        reveal: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__("", **kwargs)
        self._node = node
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._key = key or ("p", node.get("id", ""))
        self._cwd_segments = cwd_segments
        self._participant_detail = participant_detail
        self._is_first_root = is_first_root
        self._reveal = reveal
        self._frame = 0
        self._timer: Timer | None = None
        self._marquee_timer: Timer | None = None
        self._marquee_offset = 0
        self._hovered = False
        self._cursor_selected = False
        self._stage_marker: StageMarker | None = None
        self._overlay: LeafOverlay | None = None
        self.tooltip = self._tooltip_text()
        self.update(self._render_label(), layout=False)

    @property
    def key(self) -> Key:
        return self._key

    @property
    def participant_id(self) -> str | None:
        value = self._node.get("id")
        return value if isinstance(value, str) and value else None

    def _tooltip_text(self) -> str | None:
        reason = self._node.get("stage_reason")
        return reason if isinstance(reason, str) and reason else None

    def _render_label(self) -> Content:
        content = node_label(
            self._node,
            self._prefix,
            cont_prefix=self._cont_prefix,
            cwd_segments=self._cwd_segments,
            frame=self._frame,
            is_first_root=self._is_first_root,
            overlay=self._overlay,
            reveal=self._reveal,
            detail=self._visible_detail(),
        )
        if self._stage_marker is None:
            return content
        style = "$primary" if self._stage_marker == "tmux" else "$accent"
        lines = content.split("\n", allow_blank=True)
        return Content("\n").join(Content.assemble(("▌", style), " ", line) for line in lines)

    def _description(self) -> str | None:
        description = self._node.get("description")
        return description if isinstance(description, str) and description else None

    def _detail(self) -> str:
        description = self._description()
        if description is not None and (
            self._participant_detail == "description" or self._hovered or self._cursor_selected
        ):
            return description
        return shorten_path(tilde(self._node.get("cwd")), keep=self._cwd_segments)

    def _detail_width(self) -> int | None:
        if not self.is_mounted or self.content_size.width <= 0:
            return None
        gutter = 2 if self._stage_marker is not None else 0
        return max(0, self.content_size.width - cell_len(self._cont_prefix) - gutter)

    def _should_marquee(self) -> bool:
        width = self._detail_width()
        return (
            (self._hovered or self._cursor_selected)
            and self._description() is not None
            and width is not None
            and overflows_cells(self._detail(), width)
        )

    def _visible_detail(self) -> str:
        detail = self._detail()
        width = self._detail_width()
        if width is None:
            return detail
        if self._should_marquee():
            return marquee_cells(detail, width, self._marquee_offset)
        return clip_cells(detail, width)

    @property
    def required_reveal_width(self) -> int:
        content = node_label(
            self._node,
            self._prefix,
            cont_prefix=self._cont_prefix,
            cwd_segments=self._cwd_segments,
            frame=self._frame,
            is_first_root=self._is_first_root,
        )
        return max((len(line) for line in content.plain.splitlines()), default=0)

    @property
    def visible_reveal_width(self) -> int:
        return self.required_reveal_width if self._reveal is None else self._reveal

    def set_reveal(self, reveal: int | None) -> None:
        if reveal == self._reveal:
            return
        self._reveal = reveal
        self.update(self._render_label(), layout=False)

    def set_overlay(self, overlay: LeafOverlay | None) -> None:
        if overlay == self._overlay:
            return
        self._overlay = overlay or None
        self.update(self._render_label(), layout=False)

    def set_stage_marker(self, marker: StageMarker | None) -> None:
        if marker == self._stage_marker:
            return
        self._stop_marquee()
        self._stage_marker = marker
        self.update(self._render_label(), layout=False)
        self._sync_marquee()

    def retire(self) -> None:
        self.set_overlay(None)
        self.set_stage_marker(None)
        self._cursor_selected = False
        self.remove_class("tree-cursor")
        self.remove_class("tree-staged")
        self.remove_class("tree-trajectory-staged")
        self._stop_timer()
        self._stop_marquee()

    def _tick(self) -> None:
        self._frame = advance_spinner_frame(self._frame)
        self.update(self._render_label(), layout=False)

    def _start_timer(self) -> None:
        if self._timer is None:
            self._timer = self.set_interval(REGIE_LEAF_SPINNER_INTERVAL, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick_marquee(self) -> None:
        if not self._should_marquee():
            self._stop_marquee()
            self.update(self._render_label(), layout=False)
            return
        self._marquee_offset += 1
        self.update(self._render_label(), layout=False)

    def _start_marquee(self) -> None:
        if self._marquee_timer is None:
            self._marquee_timer = self.set_interval(REGIE_LEAF_MARQUEE_INTERVAL, self._tick_marquee)

    def _stop_marquee(self) -> None:
        if self._marquee_timer is not None:
            self._marquee_timer.stop()
            self._marquee_timer = None
        self._marquee_offset = 0

    def _sync_marquee(self) -> None:
        if self._should_marquee():
            self._start_marquee()
        else:
            self._stop_marquee()

    def set_cursor(self, selected: bool) -> None:
        if selected == self._cursor_selected:
            return
        self._stop_marquee()
        self._cursor_selected = selected
        self.set_class(selected, "tree-cursor")
        self.update(self._render_label(), layout=False)
        self._sync_marquee()

    def update_node(
        self,
        node: dict,
        prefix: str = "",
        *,
        cont_prefix: str = "",
        participant_detail: Literal["cwd", "description"] = "cwd",
        is_first_root: bool = False,
    ) -> None:
        detail_changed = (
            node.get("description") != self._node.get("description")
            or node.get("cwd") != self._node.get("cwd")
            or cont_prefix != self._cont_prefix
            or participant_detail != self._participant_detail
        )
        previous_label = self._render_label()
        if detail_changed:
            self._stop_marquee()
        self._node = node
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._participant_detail = participant_detail
        self._is_first_root = is_first_root
        self.tooltip = self._tooltip_text()
        label = self._render_label()
        if _content_changed(previous_label, label):
            self.update(label, layout=False)
        if node.get("status") == "working":
            self._start_timer()
        else:
            self._stop_timer()
        self._sync_marquee()

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        participant_id = self.participant_id
        select = getattr(self.app, "select_participant", None)
        if participant_id is None or not callable(select):
            return
        select(participant_id)
        if event.button == 3:
            action = getattr(self.app, "action_toggle_trajectory", None)
        elif event.button == 1 and event.chain == 1:
            action = getattr(self.app, "action_stage", None)
        else:
            return
        if callable(action):
            await action()

    def on_mount(self) -> None:
        if self._node.get("status") == "working":
            self._start_timer()
        self._sync_marquee()

    def on_unmount(self) -> None:
        self._stop_timer()
        self._stop_marquee()

    def on_enter(self, _event: events.Enter) -> None:
        self._hovered = True
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()

    def on_leave(self, _event: events.Leave) -> None:
        self._hovered = False
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()

    def on_resize(self, _event: events.Resize) -> None:
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()


__all__ = ["AgentLeaf", "StageMarker"]
