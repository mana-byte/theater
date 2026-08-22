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

from typing import ClassVar

from textual import events
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static

from theater.constants.regie import REGIE_LEAF_SPINNER_INTERVAL
from theater.regie.controllers.animation import LeafOverlay
from theater.regie.render.glyphs import node_label
from theater.regie.render.layout import Key


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
    /* Row states are tinted with $accent and $primary, never $boost.
       $boost resolves to #00000000 on 20 of Textual's 21 built-in themes —
       only textual-dark, the default, gives it a value. A $boost tint is
       therefore invisible on the ansi themes, a black smear on the light
       ones, and convincing only on the theme it was authored against.
       Weakest to strongest: hover, cursor, staged. Order matters as much
       as specificity here: these selectors tie, so the later rule wins. */
    AgentLeaf:hover {
        background: $accent 10%;
    }
    AgentLeaf.tree-staged {
        background: $primary 20%;
    }
    AgentLeaf.tree-staged:hover {
        background: $primary 20%;
    }
    AgentLeaf.tree-cursor {
        background: $accent 20%;
        text-style: bold;
    }
    AgentLeaf.tree-cursor.tree-staged {
        background: $accent 30%;
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
        self._is_first_root = is_first_root
        self._reveal = reveal
        self._frame: int = 0
        self._timer: Timer | None = None
        #: Heavy line glyphs a tree-route animation is drawing on this leaf.
        self._overlay: LeafOverlay | None = None
        # Render the initial content so the leaf is not blank before its first update_node call.
        self.update(self._render_label(), layout=False)

    @property
    def key(self) -> Key:
        return self._key

    def _render_label(self) -> Content:
        return node_label(
            self._node,
            self._prefix,
            cont_prefix=self._cont_prefix,
            cwd_segments=self._cwd_segments,
            frame=self._frame,
            is_first_root=self._is_first_root,
            overlay=self._overlay,
            reveal=self._reveal,
        )

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

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % 10
        self.update(self._render_label(), layout=False)

    def _start_timer(self) -> None:
        if self._timer is not None:
            return
        self._timer = self.set_interval(REGIE_LEAF_SPINNER_INTERVAL, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def update_node(
        self, node: dict, prefix: str = "", *, cont_prefix: str = "", is_first_root: bool = False
    ) -> None:
        """Refresh the leaf's data from a new tree tick.

        Re-renders the content, and starts or stops the spinner timer
        depending on whether the participant is now WORKING. The timer
        surviving across refreshes is the whole point of reconciliation.
        """
        self._node = node
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._is_first_root = is_first_root
        self.update(self._render_label(), layout=False)
        if node.get("status") == "working":
            self._start_timer()
        else:
            self._stop_timer()

    async def _on_click(self, event: events.Click) -> None:
        """Single click moves the cursor; double click stages.

        Staging mutates the user's tmux window layout, so it takes a
        deliberate gesture — a stray click should not join a pane. The
        check is ``event.chain >= 2``, the same discrimination vibe makes
        in its config screen.
        """
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
        if event.chain >= 2:
            await app.action_stage()

    def on_mount(self) -> None:
        if self._node.get("status") == "working":
            self._start_timer()

    def on_unmount(self) -> None:
        self._stop_timer()
