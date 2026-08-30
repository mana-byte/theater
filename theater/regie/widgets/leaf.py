"""AgentLeaf: a three-row participant leaf with its own spinner timer.

Renders three rows of Content (blank, status row, cwd row) so that WORKING
and AWAITING_INPUT are unmissable. One widget per participant — the cursor
stays 1:1 with participants, not with lines.

The animation timer is owned by the leaf itself: ``set_interval(interval,
tick)`` advances the braille spinner and working harness pulse, then calls
``update(..., layout=False)``. Started only while the participant is
WORKING, stopped when it leaves that state or on unmount. An idle régie
costs no frames.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from rich.cells import cell_len
from textual import events
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static

from theater.constants.regie import REGIE_LEAF_MARQUEE_INTERVAL, REGIE_LEAF_SPINNER_INTERVAL
from theater.formatting import tilde
from theater.regie.animations.marquee import clip_cells, marquee_cells, overflows_cells
from theater.regie.animations.routes import LeafOverlay
from theater.regie.animations.spinner import advance_spinner_frame
from theater.regie.render.glyphs import node_label
from theater.regie.render.layout import Key, shorten_path

type StageMarker = Literal["tmux", "trajectory"]


class AgentLeaf(Static):
    """A three-row participant leaf with its own spinner timer."""

    #: Text selection is disabled so dragging across leaves selects tmux output, not tree text.
    ALLOW_SELECT: ClassVar[bool] = False

    DEFAULT_CSS = """
    /* Horizontal padding only. The leaf renders exactly three rows and is
       exactly three cells tall, so vertical padding would clip the cwd row
       rather than space the leaves apart — the blank first row is already
       what separates them. Kept equal to `TreePanel > Label` so separators
       line up with the leaves they sit between, and to the bus panel's own
       inset so the two halves of the sidebar share one left margin. */
    AgentLeaf {
        height: 3;
        padding: 0 2;
        margin: 0 0;
    }
    /* Cursor and hover use fills; staged leaves draw inside their left gutter. */
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
        self._frame: int = 0
        self._timer: Timer | None = None
        self._marquee_timer: Timer | None = None
        self._marquee_offset = 0
        self._hovered = False
        self._stage_marker: StageMarker | None = None
        #: Heavy line glyphs a tree-route animation is drawing on this leaf.
        self._overlay: LeafOverlay | None = None
        # Render the initial content so the leaf is not blank before its first update_node call.
        self.update(self._render_label(), layout=False)

    @property
    def key(self) -> Key:
        return self._key

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
        if description is not None and (self._participant_detail == "description" or self._hovered):
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
            self._hovered
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
        """Largest full-text row width for startup reveal completion."""
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
        """Return the currently rendered reveal width."""
        return self.required_reveal_width if self._reveal is None else self._reveal

    def set_reveal(self, reveal: int | None) -> None:
        """Set the visible startup columns, or None for the full leaf."""
        if reveal == self._reveal:
            return
        self._reveal = reveal
        self.update(self._render_label(), layout=False)

    def set_overlay(self, overlay: LeafOverlay | None) -> None:
        """Draw (or stop drawing) the send trace on this leaf.

        A no-op when nothing changed: the animation frame touches every leaf
        it might have left, and re-rendering the untouched ones would put the
        whole tree through a repaint sixteen times a second.
        """
        if overlay == self._overlay:
            return
        self._overlay = overlay or None
        self.update(self._render_label(), layout=False)

    def set_stage_marker(self, marker: StageMarker | None) -> None:
        """Set the staged-surface marker without shifting tree content."""
        if marker == self._stage_marker:
            return
        self._stop_marquee()
        self._stage_marker = marker
        self.update(self._render_label(), layout=False)
        self._sync_marquee()

    def retire(self) -> None:
        """Stop activity while this leaf remains mounted only to shrink away."""
        self.set_overlay(None)
        self.set_stage_marker(None)
        self.remove_class("tree-cursor")
        self.remove_class("tree-staged")
        self.remove_class("tree-trajectory-staged")
        self._stop_timer()
        self._stop_marquee()

    def _tick(self) -> None:
        self._frame = advance_spinner_frame(self._frame)
        self.update(self._render_label(), layout=False)

    def _start_timer(self) -> None:
        if self._timer is not None:
            return
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

    def update_node(
        self,
        node: dict,
        prefix: str = "",
        *,
        cont_prefix: str = "",
        participant_detail: Literal["cwd", "description"] = "cwd",
        is_first_root: bool = False,
    ) -> None:
        """Refresh the leaf's data from a new tree tick.

        Re-renders the content, and starts or stops the spinner timer
        depending on whether the participant is now WORKING. The timer
        surviving across refreshes is the whole point of reconciliation.
        """
        changed = (
            node != self._node
            or prefix != self._prefix
            or cont_prefix != self._cont_prefix
            or participant_detail != self._participant_detail
        )
        if changed:
            self._stop_marquee()
        self._node = node
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._participant_detail = participant_detail
        self._is_first_root = is_first_root
        self.update(self._render_label(), layout=False)
        if node.get("status") == "working":
            self._start_timer()
        else:
            self._stop_timer()
        self._sync_marquee()

    async def _on_click(self, event: events.Click) -> None:
        """Stage and focus on left-click or toggle trajectory on right-click."""
        from theater.regie.app import RegieApp

        event.stop()
        app = self.app
        if not isinstance(app, RegieApp):
            return
        index = app._index_for_key(self._key)
        if index is None:
            return
        if app._usage_keyboard_metric is not None:
            app._leave_usage_metrics()
        app.cursor = index
        if event.button == 3:
            await app.action_toggle_trajectory()
        elif event.button == 1 and event.chain == 1:
            await app.action_stage_and_focus_tmux()

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

    def on_resize(self, _event: events.Resize) -> None:
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()
