"""Interactive three-row participant leaf."""

from __future__ import annotations

from typing import ClassVar, Literal

from rich.cells import cell_len
from textual import events
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static

from regie.animations.footer import CountingValue
from regie.animations.marquee import clip_cells, marquee_cells, overflows_cells
from regie.animations.routes import LeafOverlay
from regie.animations.spinner import advance_spinner_frame
from regie.formatting import format_cost, tilde
from regie.render.glyphs import node_label, visible_name_span
from regie.render.layout import Key, shorten_path
from regie.ui_constants import (
    REGIE_FOOTER_ANIM_INTERVAL,
    REGIE_LEAF_MARQUEE_INTERVAL,
    REGIE_LEAF_SPINNER_INTERVAL,
    REGIE_TREE_USAGE_COST_STYLE,
)
from regie.widgets.name_editor import NameEditor

type StageMarker = Literal["tmux", "trajectory"]


def _microcents(node: dict) -> int | None:
    value = node.get("usage_cost_microcents")
    return value if type(value) is int else None


def _format_leaf_cost(microcents: float) -> str:
    return format_cost(microcents, decimals=2)


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
        self._cost = CountingValue(_format_leaf_cost)
        self._cost.set_target(_microcents(node), animate=False)
        self._cost_timer: Timer | None = None
        # A row's cost counts up from zero the first time it becomes visible, as at startup.
        self._cost_shown = False
        # Textual's is_mounted is still False inside on_mount, where timers already work.
        self._mounted = False
        self._name_editor: NameEditor | None = None
        self._rename_original = ""
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
            cost=self._shown_cost(),
            width=self._label_width(),
        )
        if self._stage_marker is None:
            return content
        style = "$primary" if self._stage_marker == "tmux" else "$accent"
        lines = content.split("\n", allow_blank=True)
        return Content("\n").join(Content.assemble(("▌", style), " ", line) for line in lines)

    def _cost_in_focus(self) -> bool:
        """Only the selected or hovered agent shows its cost: the tree is not a running bill."""
        return self._cursor_selected or self._hovered

    def _shown_cost(self) -> list[str | tuple[str, str]] | None:
        if not self._cost_in_focus():
            return None
        return self._cost.parts(value_style=REGIE_TREE_USAGE_COST_STYLE)

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

    def _label_width(self) -> int | None:
        if not self.is_mounted or self.content_size.width <= 0:
            return None
        return max(0, self.content_size.width - (2 if self._stage_marker is not None else 0))

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
        self._sync_rename_geometry()

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
        self._sync_rename_geometry()
        self._sync_marquee()

    def set_usage_cost(self, microcents: int | None) -> None:
        if self._node.get("usage_cost_microcents") == microcents:
            return
        self._node["usage_cost_microcents"] = microcents
        self._retarget_cost()

    def _cost_visible(self) -> bool:
        return self._cost_in_focus() and self._mounted

    def _retarget_cost(self) -> None:
        """Count toward the new cost like the footer does, but only where it is visible."""
        counting = self._cost.set_target(
            _microcents(self._node), animate=self._cost_visible() and self._cost_shown
        )
        self._sync_cost_timer(counting)
        self._reveal_first_cost()
        self.update(self._render_label(), layout=False)

    def _sync_cost_focus(self) -> None:
        """Leaving focus settles any count; entering it may play the first count-up."""
        if not self._cost_in_focus():
            self._stop_cost_count()
            self._cost.snap()
        self._reveal_first_cost()

    def _reveal_first_cost(self) -> None:
        if self._cost_shown or not self._cost_visible() or self._cost.display is None:
            return
        self._cost_shown = True
        self._sync_cost_timer(self._cost.count_from_zero())

    def _sync_cost_timer(self, counting: bool) -> None:
        if not counting:
            self._stop_cost_count()
        elif self._cost_timer is None:
            self._cost_timer = self.set_interval(REGIE_FOOTER_ANIM_INTERVAL, self._tick_cost)

    def _tick_cost(self) -> None:
        if not self._cost.tick():
            self._stop_cost_count()
        self.update(self._render_label(), layout=False)

    def _stop_cost_count(self) -> None:
        if self._cost_timer is not None:
            self._cost_timer.stop()
            self._cost_timer = None

    def retire(self) -> None:
        self.close_rename()
        self.set_overlay(None)
        self.set_stage_marker(None)
        self._cursor_selected = False
        self.remove_class("tree-cursor")
        self.remove_class("tree-staged")
        self.remove_class("tree-trajectory-staged")
        self._stop_timer()
        self._stop_marquee()
        self._stop_cost_count()

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
        self._sync_cost_focus()
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
        previous_cost = _microcents(self._node)
        self._node = node
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._participant_detail = participant_detail
        self._is_first_root = is_first_root
        if _microcents(node) != previous_cost:
            self._retarget_cost()
        self.tooltip = self._tooltip_text()
        label = self._render_label()
        if _content_changed(previous_label, label):
            self.update(label, layout=False)
        if node.get("status") == "working":
            self._start_timer()
        else:
            self._stop_timer()
        self._sync_marquee()
        self._sync_rename_geometry()

    def _name_span(self) -> tuple[int, int] | None:
        """Column range of the row-2 name text within this leaf's content area."""
        if self._key[0] != "p":
            return None
        span = visible_name_span(self._node, self._prefix, reveal=self._reveal)
        if span is None:
            return None
        if self._stage_marker is not None:
            span = (span[0] + 2, span[1] + 2)
        return span

    def _name_clicked(self, event: events.Click) -> bool:
        span = self._name_span()
        if span is None:
            return False
        offset = event.get_content_offset(self)
        return offset is not None and offset.y == 1 and span[0] <= offset.x < span[1]

    async def begin_rename(self) -> None:
        """Open the inline editor over this row's name; managed participants only."""
        if self._key[0] != "p" or self._name_editor is not None:
            return
        participant_id = self.participant_id
        span = self._name_span() if participant_id is not None else None
        if span is None:
            return
        current = self._node.get("name")
        value = current if isinstance(current, str) and current else ""
        self._rename_original = value
        editor = NameEditor(value, submit=self._rename_submitted, cancel=self._rename_cancelled)
        self._name_editor = editor
        await self.mount(editor)
        self._sync_rename_geometry()
        editor.focus()
        editor.select_all()

    def _sync_rename_geometry(self) -> None:
        """Keep a live editor over the name as prefix, marker, reveal, or width change."""
        editor = self._name_editor
        span = self._name_span()
        if editor is None or span is None:
            return
        editor.styles.offset = (span[0], 1)
        editor.styles.width = max(12, self.content_size.width - span[0])

    def _rename_submitted(self, value: str) -> None:
        """Forward one edited name unless empty or unchanged since the editor opened."""
        self._name_editor = None
        participant_id = self.participant_id
        name = value.strip()
        if participant_id is None or not name or name == self._rename_original:
            return
        submit = getattr(self.app, "submit_rename", None)
        if callable(submit):
            submit(participant_id, name)

    def _rename_cancelled(self) -> None:
        self._name_editor = None

    def close_rename(self) -> None:
        """Detach a live editor quietly, e.g. when this row leaves the projection."""
        editor = self._name_editor
        if editor is None:
            return
        self._name_editor = None
        editor.close()

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        participant_id = self.participant_id
        if participant_id is None:
            return
        select_tree_item = getattr(self.app, "select_tree_item", None)
        if callable(select_tree_item):
            select_tree_item(self.key, participant_id)
        else:
            select_participant = getattr(self.app, "select_participant", None)
            if not callable(select_participant):
                return
            select_participant(participant_id)
        if event.button == 3:
            action = getattr(self.app, "action_toggle_trajectory", None)
        elif event.button == 1 and event.chain == 1:
            if self._name_clicked(event):
                await self.begin_rename()
                return
            action = getattr(self.app, "action_stage", None)
        else:
            return
        if callable(action):
            await action()

    def on_mount(self) -> None:
        if self._node.get("status") == "working":
            self._start_timer()
        self._sync_marquee()
        self._mounted = True
        self._reveal_first_cost()

    def on_unmount(self) -> None:
        self._mounted = False
        self._stop_timer()
        self._stop_marquee()
        self._stop_cost_count()

    def on_enter(self, _event: events.Enter) -> None:
        self._hovered = True
        self._sync_cost_focus()
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()

    def on_leave(self, _event: events.Leave) -> None:
        self._hovered = False
        self._sync_cost_focus()
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()

    def on_resize(self, _event: events.Resize) -> None:
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()
        self._sync_rename_geometry()


__all__ = ["AgentLeaf", "StageMarker"]
