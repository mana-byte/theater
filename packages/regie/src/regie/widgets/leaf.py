"""Interactive three-row participant leaf."""

from __future__ import annotations

from typing import ClassVar, Literal, cast

from rich.cells import cell_len
from textual import events
from textual.content import Content

from regie.formatting import format_cost, tilde
from regie.motions.footer import CountingValue
from regie.motions.marquee import clip_cells, marquee_cells, overflows_cells
from regie.motions.routes import LeafOverlay
from regie.motions.wipe import Wipe, apply_wipe
from regie.render.glyphs import (
    harness_shown,
    node_label,
    shown_name,
    with_gutter_bar,
    with_stage_marker,
)
from regie.render.layout import Key, shorten_path
from regie.render.row2 import Row2Layout, row2_layout
from regie.tree_nodes import RawRenderNode, RenderNode
from regie.ui_constants import (
    REGIE_FOOTER_ANIM_INTERVAL,
    REGIE_LEAF_FRAME_CYCLE,
    REGIE_LEAF_MARQUEE_INTERVAL,
    REGIE_LEAF_SPINNER_INTERVAL,
    REGIE_TREE_COST_MIN_CELLS,
    REGIE_TREE_USAGE_COST_STYLE,
)
from regie.widgets.animation_clock import AnimationSubscription, animation_clock
from regie.widgets.renameable import RenameableRow

type StageMarker = Literal["tmux", "trajectory"]

#: The bar of a row that needs the human, by breathing step (-1, 0, 1): dim, steady, bold.
_ATTENTION_BAR = {-1: "$warning dim", 0: "$warning", 1: "bold $warning"}
type LeafNode = RenderNode | dict


def _microcents(node: LeafNode) -> int | None:
    value = node.get("usage_cost_microcents")
    return value if type(value) is int else None


def _format_leaf_cost(microcents: float) -> str:
    return format_cost(microcents, decimals=2)


def _content_changed(previous: Content, current: Content) -> bool:
    return previous.plain != current.plain or previous.spans != current.spans


class AgentLeaf(RenameableRow):
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
    AgentLeaf.tree-trajectory-staged,
    AgentLeaf.tree-attention {
        padding: 0 2 0 0;
    }
    AgentLeaf.tree-cursor {
        background: $accent 20%;
        text-style: bold;
    }
    """

    def __init__(
        self,
        node: LeafNode,
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
        self._node: RawRenderNode = cast(RawRenderNode, node)
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._key = key or ("p", node.get("id", ""))
        self._cwd_segments = cwd_segments
        self._participant_detail = participant_detail
        self._is_first_root = is_first_root
        self._reveal = reveal
        self._frame = 0
        self._spinner_sub: AnimationSubscription | None = None
        self._marquee_sub: AnimationSubscription | None = None
        self._marquee_offset = 0
        self._hovered = False
        self._highlight: LeafOverlay | None = None
        self._cursor_selected = False
        self._stage_marker: StageMarker | None = None
        self._overlay: LeafOverlay | None = None
        self._wipe: Wipe | None = None
        self._attention_level = 0
        self._waiting: str | None = None
        self._awaited = False
        self._cost = CountingValue(_format_leaf_cost)
        self._cost.set_target(_microcents(node), animate=False)
        self._cost_sub: AnimationSubscription | None = None
        # A row's cost counts up from zero the first time it becomes visible, as at startup.
        self._cost_shown = False
        # Textual's is_mounted is still False inside on_mount, where timers already work.
        self._mounted = False
        self.tooltip = self._tooltip_text()
        self._sync_attention()
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
        self._row2: Row2Layout = row2_layout(
            self._node,
            self._prefix,
            width=self._label_width(),
            frame=self._frame,
            waiting=self._waiting,
            awaited=self._awaited,
            show_harness=self._harness_shown(),
            fold_hint=self._cost_in_focus(),
            focused=self._cost_in_focus(),
            reveal=self._reveal,
            marquee_offset=self._marquee_offset,
            renaming=self.renaming,
        )
        content = node_label(
            self._node,
            self._prefix,
            cont_prefix=self._cont_prefix,
            cwd_segments=self._cwd_segments,
            frame=self._frame,
            is_first_root=self._is_first_root,
            overlay=self._overlay or self._highlight,  # a live route takes over
            detail=self._visible_detail(),
            cost=self._shown_cost(),
            layout=self._row2,
        )
        if self._wipe is not None:
            content = apply_wipe(
                content,
                self._wipe,
                width=self._label_width(),
                blend=not self.app.native_ansi_color,
            )
        if self._stage_marker is None and self.needs_attention:
            return with_gutter_bar(content, _ATTENTION_BAR[self._attention_level])
        return with_stage_marker(content, self._stage_marker)

    def set_await_marks(self, waiting: str | None, awaited: bool) -> None:
        """Mark this agent blocked on a peer (in that await's colour) and/or waited on by peers."""
        if (waiting, awaited) != (self._waiting, self._awaited):
            self._waiting, self._awaited = waiting, awaited
            if self._mounted:
                self._sync_timer()
            self.update(self._render_label(), layout=False)

    def set_wipe(self, wipe: Wipe | None) -> None:
        """Draw one frame of the sweep that announces a change on this row, or end it."""
        if wipe != self._wipe:
            self._wipe = wipe
            self.update(self._render_label(), layout=False)

    @property
    def needs_attention(self) -> bool:
        return self._node.get("status") == "awaiting_input"

    def _sync_attention(self) -> None:
        if self.has_class("tree-attention") != self.needs_attention:
            self.set_class(self.needs_attention, "tree-attention")
            # The gutter moves the content box; redraw once the padding has applied.
            self.call_after_refresh(lambda: self.update(self._render_label(), layout=False))
        if not self.needs_attention:
            self._attention_level = 0

    def set_attention_level(self, level: int) -> None:
        """Breathe the attention bar: -1 dim, 0 steady, 1 bold."""
        if level != self._attention_level:
            self._attention_level = level
            if self.needs_attention:
                self.update(self._render_label(), layout=False)

    def _gutter(self) -> int:
        """Columns a bar takes from the label: the stage bar, or the bar of a waiting row."""
        return 2 if self._stage_marker is not None or self.needs_attention else 0

    def set_highlight(self, highlight: LeafOverlay | None) -> None:
        """Heavy rails leading to this agent from a hovered or selected separator above.

        A send or await route drawn along them wins: the leaf keeps plain rails while one runs.
        """
        if highlight != self._highlight:
            self._highlight = highlight
            self.update(self._render_label(), layout=False)

    def _harness_shown(self) -> bool:
        """The harness is spelled out only where the row is in focus: the name carries the rest."""
        return harness_shown(self._node, focused=self._cost_in_focus())

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

    def _cost_reserve(self) -> int:
        """Cells row three keeps for the cost of a row in focus, and the gap before it."""
        cost = self._shown_cost()
        if not cost:
            return 0
        cells = sum(cell_len(part if isinstance(part, str) else part[0]) for part in cost)
        return max(cells, REGIE_TREE_COST_MIN_CELLS) + 1

    def _detail_width(self) -> int | None:
        if not self.is_mounted or self.content_size.width <= 0:
            return None
        room = self.content_size.width - cell_len(self._cont_prefix) - self._gutter()
        return max(0, room - self._cost_reserve())

    def _label_width(self) -> int | None:
        if not self.is_mounted or self.content_size.width <= 0:
            return None
        return max(0, self.content_size.width - self._gutter())

    def _name_scrolls(self) -> bool:
        """A name wider than its room reads in full by scrolling, on the row in focus."""
        return self._row2.name_scrolls

    def _detail_scrolls(self) -> bool:
        width = self._detail_width()
        return (
            self._cost_in_focus()
            and self._description() is not None
            and width is not None
            and overflows_cells(self._detail(), width)
        )

    def _should_marquee(self) -> bool:
        return self._name_scrolls() or self._detail_scrolls()

    def _visible_detail(self) -> str:
        detail = self._detail()
        width = self._detail_width()
        if width is None:
            return detail
        if self._detail_scrolls():
            return marquee_cells(detail, width, self._marquee_offset)
        return clip_cells(detail, width)

    @property
    def required_reveal_width(self) -> int:
        node = self._node
        if self.renaming:
            # The editor draws the name; a blank of the same width keeps the row in place.
            node = {**node, "name": " " * cell_len(shown_name(node))}
        content = node_label(
            node,
            self._prefix,
            cont_prefix=self._cont_prefix,
            cwd_segments=self._cwd_segments,
            frame=self._frame,
            is_first_root=self._is_first_root,
            layout=row2_layout(
                node, self._prefix, frame=self._frame, show_harness=self._harness_shown()
            ),
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
        if counting and not self.display:
            self._settle_hidden_cost()
        elif not counting:
            self._stop_cost_count()
        elif self._cost_sub is None:
            self._cost_sub = animation_clock(self.app).subscribe(
                REGIE_FOOTER_ANIM_INTERVAL, self._tick_cost
            )

    def _settle_hidden_cost(self) -> None:
        """Hidden leaves finish the count silently: same end value, no clock wake-ups."""
        self._stop_cost_count()
        self._cost.snap()
        self.update(self._render_label(), layout=False)

    def _tick_cost(self) -> None:
        if not self._cost.tick():
            self._stop_cost_count()
        self.update(self._render_label(), layout=False)

    def _stop_cost_count(self) -> None:
        if self._cost_sub is not None:
            self._cost_sub.stop()
            self._cost_sub = None

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
        self._frame = (self._frame + 1) % REGIE_LEAF_FRAME_CYCLE
        self.update(self._render_label(), layout=False)

    def _sync_timer(self) -> None:
        """Tick while something on the row turns: the working spinner or the waiting arc."""
        if (
            self._node.get("status") == "working"
            or self._waiting is not None
            or "working" in (self._node.get("hidden_states") or {})
        ):
            self._start_timer()
        else:
            self._stop_timer()

    def _start_timer(self) -> None:
        if self._spinner_sub is None:
            self._spinner_sub = animation_clock(self.app).subscribe(
                REGIE_LEAF_SPINNER_INTERVAL, self._tick
            )

    def _stop_timer(self) -> None:
        if self._spinner_sub is not None:
            self._spinner_sub.stop()
            self._spinner_sub = None

    def _tick_marquee(self) -> None:
        if not self._should_marquee():
            self._stop_marquee()
            self.update(self._render_label(), layout=False)
            return
        self._marquee_offset += 1
        self.update(self._render_label(), layout=False)

    def _start_marquee(self) -> None:
        if self._marquee_sub is None:
            self._marquee_sub = animation_clock(self.app).subscribe(
                REGIE_LEAF_MARQUEE_INTERVAL, self._tick_marquee
            )

    def _stop_marquee(self) -> None:
        if self._marquee_sub is not None:
            self._marquee_sub.stop()
            self._marquee_sub = None
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
        self._sync_rename_geometry()  # the harness came or went: the name moved with it
        self._sync_marquee()

    def update_node(
        self,
        node: LeafNode,
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
        self._node = cast(RawRenderNode, node)
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._participant_detail = participant_detail
        self._is_first_root = is_first_root
        if _microcents(node) != previous_cost:
            self._retarget_cost()
        self.tooltip = self._tooltip_text()
        label = self._render_label()
        self._sync_attention()
        if _content_changed(previous_label, label):
            self.update(label, layout=False)
        self._sync_timer()
        self._sync_marquee()
        self._sync_rename_geometry()

    def _name_span(self) -> tuple[int, int] | None:
        """Column range of the row-2 name text within this leaf's content area."""
        if self._key[0] != "p":
            return None
        span = self._row2.name_span
        if span is None:
            return None
        gutter = self._gutter()
        return (span[0] + gutter, span[1] + gutter)

    def _fold_span(self) -> tuple[int, int] | None:
        """Columns of the fold badge: where a click folds or unfolds the children."""
        span = self._row2.fold_span if self._key[0] == "p" else None
        return None if span is None else (span[0] + self._gutter(), span[1] + self._gutter())

    def _fold_clicked(self, event: events.Click) -> bool:
        span = self._fold_span()
        offset = event.get_content_offset(self)
        return (
            span is not None
            and offset is not None
            and offset.y == 1
            and span[0] <= offset.x < span[1]
        )

    def _rename_value(self) -> str:
        current = self._node.get("name")
        return current if isinstance(current, str) and current else ""

    def _commit_rename(self, name: str) -> None:
        participant_id = self.participant_id
        submit = getattr(self.app, "submit_rename", None)
        if participant_id is not None and callable(submit):
            submit(participant_id, name)

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        participant_id = self.participant_id
        if participant_id is None:
            return
        # Measured before selecting: selecting brings the harness back and moves the name.
        left = event.button == 1 and event.chain == 1
        on_name = left and self._name_clicked(event)
        on_fold = left and self._fold_clicked(event)
        select_tree_item = getattr(self.app, "select_tree_item", None)
        if callable(select_tree_item):
            select_tree_item(self.key, participant_id)
        else:
            select_participant = getattr(self.app, "select_participant", None)
            if not callable(select_participant):
                return
            select_participant(participant_id)
        if on_fold:
            toggle = getattr(self.app, "toggle_fold", None)
            if callable(toggle):
                toggle(participant_id)
            return
        if event.button == 3:
            action = getattr(self.app, "action_toggle_trajectory", None)
        elif event.button == 1 and event.chain == 1:
            if on_name:
                await self.begin_rename()
                return
            action = getattr(self.app, "action_stage", None)
        else:
            return
        if callable(action):
            await action()

    def on_mount(self) -> None:
        self._sync_timer()
        self._sync_marquee()
        self._mounted = True
        self._reveal_first_cost()

    def on_unmount(self) -> None:
        self._mounted = False
        self._stop_timer()
        self._stop_marquee()
        self._stop_cost_count()

    def on_hide(self, _event: events.Hide) -> None:
        if self._cost_sub is not None and not self.display:
            self._settle_hidden_cost()

    def on_enter(self, _event: events.Enter) -> None:
        self._hovered = True
        self._sync_cost_focus()
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_rename_geometry()
        self._sync_marquee()

    def on_leave(self, _event: events.Leave) -> None:
        self._hovered = False
        self._sync_cost_focus()
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_rename_geometry()
        self._sync_marquee()

    def on_resize(self, _event: events.Resize) -> None:
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()
        self._sync_rename_geometry()


__all__ = ["AgentLeaf", "StageMarker"]
