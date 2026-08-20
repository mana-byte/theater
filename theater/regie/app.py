"""The régie Textual application.

Layout:
    ┌──────────────────────┬──────────────────────────┐
    │ tree (top)           │  stage                   │
    │                      │  (tmux pane, not ours)   │
    │ bus (bottom)         │                          │
    └──────────────────────┴──────────────────────────┘

The tree and bus are Textual widgets. The stage is not a widget — it is a
real tmux pane in the same window. The régie is itself a pane in that window,
and the stage occupies the space next to it. When the user selects an agent,
the daemon tells tmux to join that agent's pane into the stage window; the
régie then re-asserts its own pane layout so the tree stays visible.

Keybindings:
    j/k or up/down  navigate the tree
    Enter           stage the selected agent (join its pane into this window)
    l               stage the selected agent (if needed) and focus it
    <prefix> h      return focus to régie from the stage (claimed only if free)
    x               kill the selected agent's pane
    ctrl+p          command palette, including `Spawn <harness>`
    q               quit (unstages first; detaches, kills nothing)

Polling: the tree refreshes every 1s, the bus tail every 0.4s. Both are
async daemon calls; the app runs them as background workers. The bus is read
twice over, on two cursors: once for the panel, which must not consume rows
while it is hidden, and once for tree-route animation, which must run whether
the panel is showing or not.
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label, RichLog, Static

from theater.client import DaemonClient
from theater.config import Config, RegieSection
from theater.regie.bus_view import format_bus_line
from theater.regie.palette import (
    ResumeDeadSessionCommand,
    ResumeDeadSessionCommands,
    SpawnCommand,
    SpawnHarnessCommands,
    ViewCommands,
)
from theater.regie.tree import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    AwaitCell,
    Cell,
    Direction,
    Key,
    LeafCell,
    OverlayGlyph,
    await_highlight_cells,
    await_path,
    cell_leaf,
    is_root_prefix,
    node_label,
    render_tree,
    selected_participant,
    send_path,
    working_harness_style,
)
from theater.tmux import client as tmux
from theater.tmux import panes

logger = logging.getLogger("theater.regie")

#: Fallbacks. `config.RegieSection` owns the literals; `RegieApp` reads the
#: loaded config at start-up and overrides these per instance.
_DEFAULTS = RegieSection()

TREE_INTERVAL = _DEFAULTS.tree_interval
BUS_INTERVAL = _DEFAULTS.bus_interval
BUS_BATCH = _DEFAULTS.bus_batch

#: Note tag on the `<prefix> h` return key, so teardown can tell "ours" from
#: a binding someone else made after we installed it.
_RETURN_KEY_NOTE = "theater-regie-return"

#: How often a travelling tree trace moves. The number of moves comes from
#: the route length so long cross-root sends/spawns do not skip most of their
#: rails. A little slower than the spinners so cross-tree routes are easy to
#: follow. Not config: a constant nobody has asked to tune is not a setting.
TRACE_ANIM_INTERVAL = 0.10

#: A ceiling on concurrent traces. A busy machine can emit sends faster than
#: one animation lasts, and a hundred bright rail cells at once is noise, not signal.
MAX_TRACE_ANIMS = 6

#: Active await routes can be batched, but beyond this the tree becomes a fog.
#: The unit is handles, not `jobs.await` calls: a parent awaiting six children
#: in one call emits six `job.await.start` rows and takes six slots. Overflow
#: is permanent for that await — the daemon emits the row once and there is no
#: retry, so the thirteenth edge is simply never drawn, though the await itself
#: is unaffected and its `job.await.end` still clears whatever did get a slot.
MAX_AWAIT_ANIMS = 12

#: How long a pulse may run without the `job.await.end` row that should end it.
#: `MAX_AWAIT` in `theater.daemon.methods` caps one `jobs.await` at 300s, so a
#: pulse older than that plus a grace period is not a live await: its end row
#: was missed — `bus.tail` returns only the newest rows after the cursor and
#: silently drops the rest — or the daemon died mid-await. Not imported from
#: the daemon: the régie is a client and does not put SQLAlchemy on the import
#: path of a TUI. Without a ceiling the pulse runs forever and keeps one of
#: the MAX_AWAIT_ANIMS slots with it.
AWAIT_ANIM_TTL = 330.0

#: Heavy trace glyphs to draw within one leaf: ``(row within the leaf, column)``.
type LeafOverlay = dict[LeafCell, OverlayGlyph]

_SEND_TRACE_GLYPHS = {
    frozenset({LEFT}): "━",
    frozenset({RIGHT}): "━",
    frozenset({UP}): "┃",
    frozenset({DOWN}): "┃",
    frozenset({LEFT, RIGHT}): "━",
    frozenset({UP, DOWN}): "┃",
    frozenset({UP, RIGHT}): "┗",
    frozenset({UP, LEFT}): "┛",
    frozenset({DOWN, RIGHT}): "┏",
    frozenset({DOWN, LEFT}): "┓",
}

#: Which arms each rail glyph the tree draws actually has. A route direction
#: that is not one of them — leaving a ``└`` sideways across the blank cells
#: of a branch — moves the line but lights nothing, because there is no arm
#: there to light.
_RAIL_ARMS: dict[str, frozenset[Direction]] = {
    "│": frozenset({UP, DOWN}),
    "─": frozenset({LEFT, RIGHT}),
    "└": frozenset({UP, RIGHT}),
    "├": frozenset({UP, DOWN, RIGHT}),
}

#: The heavy form of each rail glyph, by which of its arms the await route
#: uses. Unicode has the mixed weights, so a junction the line only passes
#: through keeps its other arms light instead of being lit whole (``┠``) or
#: amputated into a plain ``┃`` — either of which claims a sibling is part of
#: a wait it knows nothing about. Left-facing mirrors (``┨┩┪``) are absent
#: because the tree draws no left-facing junction to put them on.
_AWAIT_TRACE_GLYPHS: dict[tuple[str, frozenset[Direction]], str] = {
    ("│", frozenset({UP, DOWN})): "┃",
    ("│", frozenset({UP})): "╿",
    ("│", frozenset({DOWN})): "╽",
    ("─", frozenset({LEFT, RIGHT})): "━",
    ("─", frozenset({LEFT})): "╾",
    ("─", frozenset({RIGHT})): "╼",
    ("└", frozenset({UP, RIGHT})): "┗",
    ("└", frozenset({UP})): "┖",
    ("└", frozenset({RIGHT})): "┕",
    ("├", frozenset({UP, DOWN, RIGHT})): "┣",
    ("├", frozenset({UP, DOWN})): "┠",
    ("├", frozenset({UP, RIGHT})): "┡",
    ("├", frozenset({DOWN, RIGHT})): "┢",
    ("├", frozenset({UP})): "┞",
    ("├", frozenset({DOWN})): "┟",
    ("├", frozenset({RIGHT})): "┝",
}


def _send_trace_glyph(path: list[Cell], index: int) -> str:
    """A heavy line glyph matching how the route passes through *index*."""
    row, col = path[index]
    directions: set[Direction] = set()
    for neighbor_index in (index - 1, index + 1):
        if 0 <= neighbor_index < len(path):
            next_row, next_col = path[neighbor_index]
            directions.add((next_row - row, next_col - col))
    return _SEND_TRACE_GLYPHS.get(frozenset(directions), "━")


def _await_route_glyph(glyph: str, directions: frozenset[Direction]) -> str:
    """*glyph* with the arms the route uses drawn heavy, the rest left light.

    Returns *glyph* unchanged when the route touches none of its arms, which
    is the caller's cue to leave that cell alone rather than grey out a line
    the await does not use.
    """
    arms = _RAIL_ARMS.get(glyph)
    if arms is None:
        return glyph
    return _AWAIT_TRACE_GLYPHS.get((glyph, directions & arms), glyph)


def _await_route_style(frame: int, offset: int = 0) -> str:
    """The working harness grayscale, at this cell's place along the route.

    No ``bold``: the glyphs are already the heavy box-drawing forms, and bold
    promotes a grey into the bright ANSI palette on some terminals — which
    turns the one thing this style is for, a line dimmer than a live agent,
    into a line brighter than one.
    """
    return working_harness_style(frame, offset)


def _is_participant_key(key: Key) -> bool:
    """Whether *key* identifies a participant or unmanaged pane (not a separator)."""
    return key[0] in ("p", "u")


class RouteAnim:
    """One trace travelling from a sender's leaf to its target's.

    Holds the two participant ids and how many route cells it has travelled —
    never the route itself. The tree refreshes underneath it every second, and
    a stored route would go stale the moment an agent above it dies and every
    row shifts up. Recomputing per frame means the trace lands somewhere
    sensible even if the path changed length, and disappears cleanly the moment
    either end stops being visible.
    """

    def __init__(self, from_id: str, to_id: str) -> None:
        self.from_id = from_id
        self.to_id = to_id
        self.step = 0


class AwaitRouteAnim:
    """One active await relationship pulsing along a visible tree route.

    Carries its own deadline. The pulse is supposed to end on a matching
    `job.await.end` row, and mostly does — but a row that never arrives would
    otherwise leave it pulsing for the rest of the session, so it also expires
    on its own after :data:`AWAIT_ANIM_TTL`.
    """

    def __init__(
        self, token: str, handle: str, from_id: str, to_id: str, started: float | None = None
    ) -> None:
        self.token = token
        self.handle = handle
        self.from_id = from_id
        self.to_id = to_id
        self.frame = 0
        self.started = time.monotonic() if started is None else started

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.token, self.handle, self.from_id, self.to_id)

    def expired(self, now: float) -> bool:
        return now - self.started >= AWAIT_ANIM_TTL


class AgentLeaf(Static):
    """A three-row participant leaf with its own spinner timer.

    Renders three rows of Content (blank, status row, cwd row) so that
    WORKING and AWAITING_INPUT are unmissable. One widget per participant —
    the cursor stays 1:1 with participants, not with lines.

    The animation timer is owned by the leaf itself:
    ``set_interval(0.1, tick)`` advances the braille spinner and working
    harness pulse, then calls ``update(..., layout=False)``. Started only while
    the participant is WORKING, stopped when it leaves that state or on
    unmount. An idle régie costs no frames. This is vibe's pattern: the timer
    lives on the widget that needs it, not on the app.
    """

    #: Text selection is disabled so dragging across leaves selects tmux
    #: output in the stage, not the tree's own text.
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
        **kwargs,
    ) -> None:
        super().__init__("", **kwargs)
        self._node = node
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._key = key or ("p", node.get("id", ""))
        self._cwd_segments = cwd_segments
        self._is_first_root = is_first_root
        self._frame: int = 0
        self._timer: Timer | None = None
        #: Heavy line glyphs a tree-route animation is currently drawing on this
        #: leaf. Owned by the panel, which sets and clears them every frame.
        self._overlay: LeafOverlay | None = None
        # Render the initial content so the leaf is not blank before its
        # first update_node call.
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
        )

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
        self._timer = self.set_interval(0.1, self._tick)

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
        event.stop()
        app = self.app
        if not isinstance(app, RegieApp):
            return
        index = app._index_for_key(self._key)
        if index is None:
            return
        app.cursor = index
        if event.chain >= 2:
            await app.action_stage()

    def on_mount(self) -> None:
        if self._node.get("status") == "working":
            self._start_timer()

    def on_unmount(self) -> None:
        self._stop_timer()


class TreePanel(VerticalScroll):
    """A scrollable list of participant tree leaves.

    Each participant is an :class:`AgentLeaf` widget spanning three rows.
    Separator rows and the "no participants" placeholder stay plain
    ``Label`` widgets. The panel scrolls natively when the list is longer
    than the viewport. Cursor and staged highlighting are done via Textual
    CSS classes (which respect the user's theme) rather than hardcoded Rich
    colours.

    The panel reconciles by key rather than rebuilding on every refresh: a
    widget that survives a tick is updated in place with
    ``AgentLeaf.update_node`` (or ``Label.update`` for separators) so that
    per-widget state (a hover class, an animation timer) is not destroyed.
    Widgets for rows that have disappeared are removed, and widgets for
    new rows are mounted at the right position. Final child order always
    matches the row order.

    Removal is async in Textual — ``Widget.remove`` returns an
    ``AwaitRemove`` and the widget stays in ``self.children`` until the
    event loop pumps. The panel never depends on ``self.children`` for
    positional indexing: ``apply_cursor`` and ``scroll_to_cursor`` resolve
    widgets by key through ``self._key_widgets``, and ordering uses
    ``move_child`` with widget references rather than integer indices. A
    removed-but-not-yet-pumped widget still sitting in the list therefore
    cannot corrupt the highlight or the scroll target.
    """

    lines: reactive[list[tuple[Content, dict, Key, str, str]]] = reactive([])

    #: Key for the placeholder shown when the tree is empty.
    _EMPTY_KEY: Key = ("empty", "")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        #: Set by the app so apply_cursor can map a line index back to its
        #: node. Per-instance: the app rebinds it on every redraw.
        self._lines_data: list[tuple[Content, dict, Key, str, str]] = []
        #: Stable widget map, keyed by the row key from render_tree.
        #: Survives a refresh so per-widget state is not lost. The value is
        #: ``AgentLeaf`` for participants and unmanaged panes, ``Label`` for
        #: the separator and the empty placeholder — both are ``Widget``
        #: subclasses, so the annotation is widened honestly.
        self._key_widgets: dict[Key, Widget] = {}
        #: Which leaves currently carry a send trace, so the next frame knows
        #: which ones to clear. Tracking the request rather than the widget:
        #: a leaf that has since been unmounted took its overlay with it.
        self._overlaid: set[Key] = set()

    DEFAULT_CSS = """
    TreePanel {
        height: 1fr;
        scrollbar-size: 0 0;
    }
    /* Matches AgentLeaf's padding on purpose: these are the separator and
       placeholder rows, and a different inset would step them out of line
       with the leaves above and below. */
    TreePanel > Label {
        height: 1;
        padding: 0 2;
        margin: 0 0;
    }
    """

    def watch_lines(self, lines: list[tuple[Content, dict, Key, str, str]]) -> None:
        """Reconcile widget children with the new row list.

        Existing widgets are updated in place; only new keys are mounted and
        only gone keys are unmounted. Final child order matches the row
        order, driven by widget references rather than integer indices so
        that a pending async removal cannot corrupt the arithmetic.

        Participant rows become ``AgentLeaf`` widgets with their own spinner
        timers; separator and placeholder rows stay plain ``Label`` widgets.
        """
        if not lines:
            self._reconcile_empty()
            return

        new_key_set = {key for _, _, key, _, _ in lines}

        if self._EMPTY_KEY in self._key_widgets:
            self._remove_widget(self._key_widgets.pop(self._EMPTY_KEY))

        for key in list(self._key_widgets):
            if key not in new_key_set:
                self._remove_widget(self._key_widgets.pop(key))

        # TreePanel.app is RegieApp at runtime; Widget.app is typed App[Any].
        cwd_segments = self.app.settings.regie.cwd_segments  # type: ignore[attr-defined]
        ordered_widgets: list[Widget] = []
        participant_index = 0
        for i, (label, node, key, prefix, cont_prefix) in enumerate(lines):
            widget = self._reconcile_row(label, node, key, prefix, cont_prefix, cwd_segments, i)
            if _is_participant_key(key):
                widget.set_class(participant_index % 2 == 0, "tree-alt")
                participant_index += 1
            ordered_widgets.append(widget)

        # move_child is synchronous and takes widget references, so a
        # pending async removal cannot shift the indexing — there is none.
        for i, widget in enumerate(ordered_widgets):
            if i == 0:
                continue
            self.move_child(widget, after=ordered_widgets[i - 1])

    def _reconcile_row(
        self,
        label: Content,
        node: dict,
        key: Key,
        prefix: str,
        cont_prefix: str,
        cwd_segments: int,
        index: int = 0,
    ) -> Widget:
        """Update or create the widget for a single row.

        Existing ``AgentLeaf`` widgets are updated in place with
        ``update_node``; existing ``Label`` widgets (separators) with
        ``update``. New participant rows get a fresh ``AgentLeaf``, new
        separator rows get a plain ``Label``.

        *index* is the row's position in the flat line list, used to identify
        the first root. The alternating ``tree-alt`` class is recomputed every
        tick in ``_reconcile`` based on the participant index, so insertions
        and removals keep the zebra stripe correct.
        The *is_first_root* flag is recomputed every tick and threaded to the
        leaf so its spinner re-renders also blank row 1 for the first root.
        """
        first_root = index == 0 and is_root_prefix(prefix)
        if key in self._key_widgets:
            widget = self._key_widgets[key]
            if isinstance(widget, AgentLeaf):
                widget.update_node(
                    node, prefix=prefix, cont_prefix=cont_prefix, is_first_root=first_root
                )
            elif isinstance(widget, Label):
                widget.update(label)
            return widget
        if _is_participant_key(key):
            widget = AgentLeaf(
                node,
                prefix,
                cont_prefix=cont_prefix,
                key=key,
                cwd_segments=cwd_segments,
                is_first_root=first_root,
            )
        else:
            widget = Label(label)
        self._key_widgets[key] = widget
        self.mount(widget)
        return widget

    def _reconcile_empty(self) -> None:
        """Show the 'no participants' placeholder, removing all row widgets."""
        for key in list(self._key_widgets):
            if key != self._EMPTY_KEY:
                self._remove_widget(self._key_widgets.pop(key))
        if self._EMPTY_KEY not in self._key_widgets:
            widget = Label(Content.assemble(("no participants", "$text dim italic")))
            self._key_widgets[self._EMPTY_KEY] = widget
            self.mount(widget)

    def _remove_widget(self, widget: Widget) -> None:
        """Remove a widget via Textual's public API.

        ``widget.remove()`` returns an ``AwaitRemove`` and the widget stays
        in ``self.children`` until the event loop pumps. That is fine: the
        panel never indexes ``self.children`` positionally. Cursor and
        staged state are resolved by key through ``self._key_widgets``, and
        ordering uses widget references, so a pending removal cannot land
        the highlight on the wrong row.
        """
        widget.remove()

    def apply_cursor(self, cursor: int, staged_pane: str | None) -> None:
        """Add CSS classes to the cursor and staged lines, remove from others.

        Widgets are resolved by key through ``self._key_widgets`` rather
        than by position in ``self.children``. A pending async removal may
        leave a stale widget in ``self.children`` for one frame; resolving
        by key means the highlight always lands on the right row.
        """
        for i, (_, node, key, _, _) in enumerate(self._lines_data):
            widget = self._key_widgets.get(key)
            if widget is None:
                continue
            widget.remove_class("tree-cursor")
            widget.remove_class("tree-staged")
            if staged_pane and node.get("tmux_pane") == staged_pane:
                widget.add_class("tree-staged")
            if i == cursor:
                widget.add_class("tree-cursor")

    def set_overlays(self, overlays: dict[Key, LeafOverlay]) -> None:
        """Put the send trace on the leaves that carry it, clear the rest.

        Called once per animation frame with the whole picture rather than
        per send, so a leaf the animation has moved off is cleared in the
        same pass that draws where it moved to. An empty mapping is the
        normal way an animation ends.
        """
        for key in self._overlaid - set(overlays):
            widget = self._key_widgets.get(key)
            if isinstance(widget, AgentLeaf):
                widget.set_overlay(None)
        for key, cells in overlays.items():
            widget = self._key_widgets.get(key)
            if isinstance(widget, AgentLeaf):
                widget.set_overlay(cells)
        self._overlaid = set(overlays)

    def scroll_to_cursor(self, cursor: int) -> None:
        """Ensure the cursor line is visible."""
        if cursor < 0 or cursor >= len(self._lines_data):
            return
        key = self._lines_data[cursor][2]
        widget = self._key_widgets.get(key)
        if widget is not None:
            with contextlib.suppress(Exception):
                self.scroll_to_widget(widget)


def _fmt_tokens(n: int) -> str:
    """Human-readable token count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


class UsageFooter(Widget):
    """Footer showing aggregate token/cost usage."""

    DEFAULT_CSS = ""
    totals: reactive[dict | None] = reactive(None)

    def render(self) -> Content:
        t = self.totals
        if not isinstance(t, dict):
            return Content.assemble(("  —", "$text"))
        inp = _fmt_tokens(t.get("input_tokens", 0))
        out = _fmt_tokens(
            t.get("output_tokens", 0) + t.get("reasoning_output_tokens", 0)
        )
        cache = _fmt_tokens(
            t.get("cache_read_input_tokens", 0) + t.get("cache_creation_input_tokens", 0)
        )
        cost = t.get("cost_microcents", 0) / 100_000_000.0
        return Content.assemble(
            (f"  {inp} in  {out} out  {cache} cache  ${cost:.2f}", "$text bold")
        )


class RegieApp(App):
    """The theater control panel."""

    CSS = """
    Screen {
        layout: horizontal;
    }
    #sidebar {
        layout: vertical;
    }
    #tree-panel {
        height: 1fr;
    }
    #bus-panel {
        height: 18;
        padding: 1 2;
        scrollbar-size: 0 0;
    }
    #bus-panel.-hidden {
        display: none;
    }
    /* The zebra stripe and its row states. These live in App.CSS, which
       outranks AgentLeaf.DEFAULT_CSS whatever the specificity, so every
       state an alt row can be in has to be restated here or the widget's
       own cursor rule never applies to every other row. The stripe is a
       3% ink wash: $foreground darkens light themes and lightens dark
       ones, and 3% keeps it below the hover tint on all 21 themes. */
    AgentLeaf.tree-alt {
        background: $foreground 3%;
    }
    AgentLeaf.tree-alt:hover {
        background: $accent 10%;
    }
    AgentLeaf.tree-alt.tree-staged {
        background: $primary 20%;
    }
    AgentLeaf.tree-alt.tree-staged:hover {
        background: $primary 20%;
    }
    AgentLeaf.tree-alt.tree-cursor {
        background: $accent 20%;
        text-style: bold;
    }
    AgentLeaf.tree-alt.tree-cursor.tree-staged {
        background: $accent 30%;
        text-style: bold;
    }
    .log {
        background: $surface;
    }
    UsageFooter {
        dock: none;
        height: 1;
        background: $surface;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("up", "cursor_up", "up", show=False),
        Binding("enter", "stage", "stage"),
        Binding("l", "focus_stage", "focus", show=False),
        Binding("x", "kill", "kill"),
        Binding("q", "quit", "quit"),
    ]

    #: ctrl+p opens the palette. Ours adds one `Spawn <harness>` entry per
    #: registered harness on top of Textual's system commands.
    COMMANDS = App.COMMANDS | {SpawnCommand, ViewCommands, ResumeDeadSessionCommand}

    title = "theater régie"

    cursor: reactive[int] = reactive(0)
    tree_lines: reactive[list[tuple[Content, dict, Key, str, str]]] = reactive([])
    bus_cursor: int = 0
    #: The tree-route animation's own place in the bus. Separate from `bus_cursor`
    #: on purpose: the panel must not consume rows it cannot display while
    #: hidden, and the animation must run whether it is hidden or not. Two
    #: readers of one log, each keeping its own place.
    anim_cursor: int = 0
    #: Whether the animation poll has seen the log once. The first poll only
    #: takes the cursor: without it, starting the régie would replay every
    #: send still in the daemon's buffer as if it had just happened.
    _anim_primed: bool = False
    #: Whether the bus panel is showing. Toggled from the palette, not a key:
    #: a once-a-session decision. Hiding it pauses the poll (see _refresh_bus).
    bus_visible: reactive[bool] = reactive(False)
    #: The régie's own pane id (from $TMUX_PANE), discovered at mount.
    my_pane: str | None = None
    #: The window id the régie lives in, discovered at mount.
    my_window: str | None = None
    #: The pane id currently on stage (joined into our window), or None.
    staged_pane: reactive[str | None] = reactive(None)
    #: The session the régie is running in. tmux options are scoped to it.
    my_session: str | None = None
    #: The session by name. The spawner matches list-sessions output, which
    #: is names, so the `$id` form is no use to it.
    my_session_name: str | None = None
    #: The session-local value of tmux's `mouse` option before we changed it,
    #: or None if the session had no override of its own.
    _mouse_prev: str | None = None
    #: Whether we actually changed the option, so a failed enable does not
    #: cause a restore that clobbers a setting we never touched.
    _mouse_set: bool = False
    #: The session-local value of tmux's `status` option before we hid it,
    #: or None if the session had no override of its own.
    _status_prev: str | None = None
    #: Whether we actually hid the status line, on the same terms as
    #: `_mouse_set`: a failed hide must not trigger a restore.
    _status_set: bool = False
    #: Teardown runs from two places and must not run twice.
    _torn_down: bool = False
    #: Whether we actually installed the `<prefix> h` return key (vs. the
    #: user already having one), so teardown only removes what we added.
    _return_key_set: bool = False

    def __init__(self, settings: Config | None = None):
        super().__init__()
        self._client: DaemonClient | None = None
        #: The whole config, not just [regie]: the palette needs
        #: theater.favourite. Injectable for tests.
        self.settings = settings or Config()
        #: What the daemon says it can spawn, or None (before mount or on
        #: failure). The palette reads None as "ask the local registry".
        self.harnesses: list[dict] | None = None
        self.bus_visible = self.settings.regie.bus_visible
        #: Tree-route traces in flight. Concurrent rather than queued: a queue
        #: would show a route after the event it represents, which is a lie
        #: about when it happened.
        self._route_anims: list[RouteAnim] = []
        #: Await routes that should pulse until the daemon says the await call
        #: returned. Keyed by the daemon's await token plus handle.
        self._await_anims: dict[tuple[str, str, str, str], AwaitRouteAnim] = {}
        #: Bumped whenever `tree_lines` is replaced. An await route is a
        #: property of the drawn tree, which changes once a second, and it is
        #: asked for ten times a second — so it is computed against this and
        #: reused until the tree moves under it.
        self._tree_revision = 0
        self._await_cells: dict[tuple[str, str], list[AwaitCell] | None] = {}
        self._await_cells_revision = -1
        #: Runs only while something is in flight — an idle régie costs no
        #: frames, the same bargain AgentLeaf's spinner makes.
        self._anim_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        # No Header: it restates the app's class name to someone who just
        # typed the command that started it. The footer shows usage totals.
        with Vertical(id="sidebar"):
            yield TreePanel(id="tree-panel")
            yield UsageFooter(id="usage-footer")
            bus = RichLog(id="bus-panel", max_lines=200, wrap=False, markup=True)
            # Applied here, not in watch_bus_visible: a reactive assigned
            # its own default fires no watcher, so false-vs-false would
            # mount the panel visible and never correct it.
            bus.set_class(not self.bus_visible, "-hidden")
            yield bus

    async def on_mount(self) -> None:
        self._client = DaemonClient()
        await self._client.connect()
        # Width is imperative, not CSS: App.CSS is parsed once and cannot
        # read config. The same value is used in action_stage; the two must
        # agree or Textual and tmux disagree about the sidebar edge.
        self.query_one("#sidebar").styles.width = self.settings.regie.sidebar_width
        # Discover our own pane and window: staging joins another pane
        # into this window, so we need to know which one we are in.
        my_pane = tmux.current_pane()
        if my_pane:
            self.my_pane = my_pane
            try:
                self.my_window = await tmux.display_message("#{window_id}", target=my_pane)
                self.my_session = await tmux.display_message("#{session_id}", target=my_pane)
                self.my_session_name = await tmux.display_message("#{session_name}", target=my_pane)
            except Exception as exc:
                logger.debug("could not discover window/session id: %s", exc)
            await self._bind_return_key()
        await self._enable_mouse()
        await self._hide_status()
        self._apply_theme()
        await self._load_harnesses()
        self.set_interval(self.settings.regie.tree_interval, self._refresh_tree)
        self.set_interval(self.settings.regie.bus_interval, self._refresh_bus)
        self.set_interval(self.settings.regie.bus_interval, self._refresh_anim)
        await self._refresh_tree()
        await self._refresh_bus()
        # Primes the animation cursor: whatever is already in the log
        # happened before the régie was looking, and is not news.
        await self._refresh_anim()

    async def _load_harnesses(self) -> None:
        """Ask the daemon what it can spawn, for the palette to offer.

        Once at mount, not per keystroke: the palette opens on ctrl+p and must
        be instant, and the answer only changes when the daemon restarts — it
        reads its config at start and never reloads. Failure leaves
        `self.harnesses` at None, which the palette reads as "use the local
        registry" rather than showing an empty list.
        """
        if self._client is None:
            return
        try:
            rows = await self._client.call("harnesses")
            assert isinstance(rows, list)
        except Exception as exc:
            logger.debug("harness list unavailable: %s", exc)
            return
        self.harnesses = rows

    def _apply_theme(self) -> None:
        """Switch to the configured theme, or say why not.

        Unknown names are the one config error not raised at load time: the
        legal values live inside Textual, and importing the TUI stack into the
        daemon to validate a string is a poor trade. So the check happens here,
        where the real alternatives can be listed — and it is a notification
        rather than a crash, because a bad theme name is a cosmetic mistake and
        killing the régie over it would hide every agent on the machine.
        """
        name = self.settings.regie.theme
        if not name:
            return
        available = sorted(self.available_themes)
        if name not in available:
            self.notify(
                f"unknown theme {name!r} — available: {', '.join(available)}",
                title="config",
                severity="warning",
                timeout=10,
            )
            return
        self.theme = name

    async def on_unmount(self) -> None:
        # Best effort: q / ctrl-c already tore down in action_quit where
        # awaiting works; this catches paths that never reach it.
        await self._teardown()
        if self._client:
            await self._client.aclose()

    # ---- tmux lifecycle -------------------------------------------------

    async def _enable_mouse(self) -> None:
        """Turn tmux mouse reporting on for the régie's session.

        Scoped to the session rather than `-g`: a global set would change every
        session on the server and outlive this process, which is not a choice a
        TUI gets to make on the user's behalf. The previous session-local value
        is remembered so exiting puts it back.
        """
        if not self.my_session:
            return
        try:
            self._mouse_prev = await tmux.show_option("mouse", target=self.my_session)
            await tmux.set_option("mouse", "on", target=self.my_session)
            self._mouse_set = True
        except Exception as exc:
            logger.debug("could not enable mouse: %s", exc)

    async def _restore_mouse(self) -> None:
        if not self._mouse_set or not self.my_session:
            return
        self._mouse_set = False
        try:
            if self._mouse_prev is None:
                # No prior override: remove ours rather than pinning to the
                # global value.
                await tmux.unset_option("mouse", target=self.my_session)
            else:
                await tmux.set_option("mouse", self._mouse_prev, target=self.my_session)
        except Exception as exc:
            logger.debug("could not restore mouse: %s", exc)

    async def _hide_status(self) -> None:
        """Hide tmux's own status line while the régie is up.

        The régie already draws a footer with usage totals, and the
        rest of the window is the stage — a real agent pane. tmux's status
        bar underneath duplicates neither and costs a row of a terminal the
        stage wants. Scoped to the session and remembered on exactly the
        terms `_enable_mouse` uses: a `-g` set would change every session on
        the server and outlive this process.
        """
        if not self.my_session:
            return
        try:
            self._status_prev = await tmux.show_option("status", target=self.my_session)
            await tmux.set_option("status", "off", target=self.my_session)
            self._status_set = True
        except Exception as exc:
            logger.debug("could not hide status line: %s", exc)

    async def _restore_status(self) -> None:
        if not self._status_set or not self.my_session:
            return
        self._status_set = False
        try:
            if self._status_prev is None:
                # No prior override: remove ours rather than pinning to the
                # global value.
                await tmux.unset_option("status", target=self.my_session)
            else:
                await tmux.set_option("status", self._status_prev, target=self.my_session)
        except Exception as exc:
            logger.debug("could not restore status line: %s", exc)

    async def _bind_return_key(self) -> None:
        """Claim `<prefix> h` for `select-pane -L`, unless the user already has it.

        Has to be a tmux binding, not a Textual one: once the staged pane has
        tmux's focus, régie gets no keystrokes at all. The stage always sits
        to the right of régie, so "move left" is "return to régie" — no
        hardcoded pane id needed, and if the user's own config already binds
        `h` this way, it already does what we want.
        """
        try:
            self._return_key_set = await tmux.bind_key_if_free(
                "prefix", "h", ["select-pane", "-L"], note=_RETURN_KEY_NOTE
            )
        except Exception as exc:
            logger.debug("could not bind <prefix> h return key: %s", exc)

    async def _unbind_return_key(self) -> None:
        if not self._return_key_set:
            return
        self._return_key_set = False
        try:
            await tmux.unbind_key_if_owned("prefix", "h", note=_RETURN_KEY_NOTE)
        except Exception as exc:
            logger.debug("could not unbind <prefix> h return key: %s", exc)

    async def _teardown(self) -> None:
        """Leave tmux as we found it: nothing staged, options restored.

        Unstaging matters more than it looks. The staged agent's pane lives in
        the régie's window only for as long as the régie is there to frame it;
        quitting without breaking it out would leave the agent alive but sharing
        a window with a dead TUI. `break_pane` moves it back to a window of its
        own without touching the process.
        """
        if self._torn_down:
            return
        self._torn_down = True
        pane = self.staged_pane
        if pane:
            try:
                await panes.break_pane(pane)
            except Exception as exc:
                logger.debug("unstage on exit failed: %s", exc)
        await self._restore_mouse()
        await self._restore_status()
        await self._unbind_return_key()

    async def action_quit(self) -> None:
        """Quit, but put the stage back first.

        This has to happen here and not only in `on_unmount`: by unmount time
        Textual is already shutting the loop down, and an awaited tmux call can
        be cancelled halfway through — which would strand the staged agent in a
        window whose other occupant has exited.
        """
        await self._teardown()
        self.exit()

    # ---- polling -------------------------------------------------------

    async def _refresh_tree(self) -> None:
        if not self._client:
            return
        try:
            tree = await self._client.call("participants.tree")
            assert isinstance(tree, list)
            unmanaged = await self._client.call("participants.unmanaged")
            assert isinstance(unmanaged, list)
        except Exception as exc:
            logger.debug("tree refresh failed: %s", exc)
            return
        self.tree_lines = render_tree(
            tree, unmanaged, cwd_segments=self.settings.regie.cwd_segments
        )
        self._tree_revision += 1
        if self.cursor >= len(self.tree_lines):
            self.cursor = max(0, len(self.tree_lines) - 1)
        self._render_tree()
        with contextlib.suppress(Exception):
            totals = await self._client.call("usage_totals")
            assert isinstance(totals, dict)
            self.query_one("#usage-footer", UsageFooter).totals = totals

    async def _refresh_bus(self) -> None:
        if not self._client:
            return
        if not self.bus_visible:
            # A display:none RichLog accepts writes and keeps none of them.
            # Polling on would advance the cursor past never-rendered lines.
            # Leaving it means resuming from the last drawn line, and the gap
            # check below says so if the daemon's buffer wrapped while hidden.
            return
        try:
            rows = await self._client.call(
                "bus.tail", limit=self.settings.regie.bus_batch, after_id=self.bus_cursor
            )
            assert isinstance(rows, list)
        except Exception as exc:
            logger.debug("bus refresh failed: %s", exc)
            return
        if not rows:
            return
        # bus.tail returns newest N after cursor (DESC); reverse for display
        # if there's a gap.
        if rows[0]["id"] > self.bus_cursor + 1 and self.bus_cursor > 0:
            missed = rows[0]["id"] - self.bus_cursor - 1
            log = self.query_one("#bus-panel", RichLog)
            log.write(Text(f"... {missed} events dropped", style="dim italic"))
        rows_sorted = sorted(rows, key=lambda r: r["id"])
        log = self.query_one("#bus-panel", RichLog)
        # Resolved per batch: the palette can switch themes mid-session, and
        # lines written after that should follow it. Already-written lines
        # keep their old colour — the price of a RichLog, cheaper than
        # re-rendering on every tick.
        variables = self.theme_variables
        for row in rows_sorted:
            log.write(format_bus_line(row, variables=variables))
        self.bus_cursor = rows[-1]["id"]

    # ---- tree-route animation -------------------------------------------

    async def _refresh_anim(self) -> None:
        """Read the bus for visible tree-route events, hidden panel or not.

        A second reader of the same log rather than a hook in `_refresh_bus`,
        because the two want opposite things from a hidden panel: the panel
        must not advance past rows it never drew, and the animation must not
        stop just because nobody is reading the text. Its own cursor is the
        whole of that separation, and this method never writes to the panel.

        `agent.send` is the send trigger rather than `job.created` because it
        is emitted after the keystrokes reached the target's pane — the trace
        stands for delivery, not for intent. `participant.created` is the spawn
        trigger; it carries parent -> child and causes an immediate tree refresh
        because the child may not be in the current render yet. `job.await.*`
        brackets an active await call, so the route can pulse only while the
        caller is actually blocked on the target.

        A batch that needs the tree refreshed gets exactly one refresh, before
        any of its rows are turned into animations. Refreshing per row was one
        daemon round-trip per row: a burst of spawns then paid for the same
        answer several times over, in sequence, on the frame that could least
        afford it.
        """
        if not self._client:
            return
        try:
            rows = await self._client.call(
                "bus.tail", limit=self.settings.regie.bus_batch, after_id=self.anim_cursor
            )
            assert isinstance(rows, list)
        except Exception as exc:
            logger.debug("tree route animation poll failed: %s", exc)
            return
        if rows:
            self.anim_cursor = max(int(row["id"]) for row in rows)
        if not self._anim_primed:
            self._anim_primed = True
            return
        if any(self._needs_tree_refresh(row) for row in rows):
            await self._refresh_tree()
        for row in rows:
            payload = row.get("payload") or {}
            # A spawn carrying a prompt is a send in all but name: the parent
            # handed the child something to do, and the trace says so.
            if row.get("kind") == "agent.send" or self._is_prompted_spawn(row):
                self.start_route_anim(row.get("from_id"), row.get("to_id"))
            elif row.get("kind") == "job.await.start":
                self.start_await_anim(
                    payload.get("token"),
                    payload.get("handle"),
                    row.get("from_id"),
                    row.get("to_id"),
                )
            elif row.get("kind") == "job.await.end":
                self.stop_await_anim(
                    payload.get("token"),
                    payload.get("handle"),
                    row.get("from_id"),
                    row.get("to_id"),
                )

    @staticmethod
    def _is_prompted_spawn(row: dict) -> bool:
        """Whether *row* is a child created with a prompt from a visible parent."""
        payload = row.get("payload") or {}
        return bool(
            row.get("kind") == "participant.created"
            and row.get("from_id")
            and payload.get("has_prompt") is True
        )

    @classmethod
    def _needs_tree_refresh(cls, row: dict) -> bool:
        """Whether *row* animates something the current render may not hold yet.

        A spawn's child and an await's target can both be newer than the last
        tree poll, and an animation with no row to start from is dropped
        silently — so these two kinds are worth a refresh before they are read.
        Asked of the whole batch at once: one round-trip answers for all of it,
        and a burst of spawns used to pay for the same answer several times
        over, in sequence, on the frame that could least afford it.
        """
        return row.get("kind") == "job.await.start" or cls._is_prompted_spawn(row)

    def start_route_anim(self, from_id: str | None, to_id: str | None) -> None:
        """Begin a trace travelling from *from_id* to *to_id*, if it can.

        Silently declines when there is no route: a send from the CLI, from an
        external agent with no row, or to a participant that has already left
        the tree has no visible route to draw. The visualisation is decoration —
        the one thing it must never do is complain.
        """
        if len(self._route_anims) >= MAX_TRACE_ANIMS:
            return
        if send_path(self.tree_lines, from_id, to_id) is None:
            return
        assert from_id and to_id  # send_path returned a route, so both exist
        self._route_anims.append(RouteAnim(from_id, to_id))
        if self._anim_timer is None:
            self._anim_timer = self.set_interval(TRACE_ANIM_INTERVAL, self._tick_route_anims)

    def start_await_anim(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> None:
        """Begin a grayscale pulse for an await edge, if both ends are visible.

        Expired pulses are reaped first: a slot held by an await whose end row
        never came must not be what turns a real one away.
        """
        self._reap_await_anims()
        if len(self._await_anims) >= MAX_AWAIT_ANIMS:
            return
        if not token or not handle or not from_id or not to_id or from_id == to_id:
            return
        if await_path(self.tree_lines, from_id, to_id) is None:
            return
        anim = AwaitRouteAnim(str(token), str(handle), from_id, to_id)
        self._await_anims[anim.key] = anim
        if self._anim_timer is None:
            self._anim_timer = self.set_interval(TRACE_ANIM_INTERVAL, self._tick_route_anims)

    def stop_await_anim(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> None:
        """End one active await pulse and clear it if it was the last overlay."""
        if not token or not handle or not from_id or not to_id:
            return
        self._await_anims.pop((str(token), str(handle), from_id, to_id), None)
        if self._route_anims or self._await_anims:
            return
        panel = self._panel()
        if panel is not None:
            panel.set_overlays({})
        self._stop_anim_timer()

    def _reap_await_anims(self) -> None:
        """Drop pulses whose end row never arrived. See :data:`AWAIT_ANIM_TTL`."""
        now = time.monotonic()
        for key, anim in list(self._await_anims.items()):
            if anim.expired(now):
                del self._await_anims[key]

    def _await_route_cells(self, from_id: str, to_id: str) -> list[AwaitCell] | None:
        """The await route's visible cells, computed once per tree revision.

        The tree is redrawn once a second and the pulse is drawn ten times a
        second, so nine frames in ten would otherwise re-run a breadth-first
        search over a grid that has not moved.
        """
        if self._await_cells_revision != self._tree_revision:
            self._await_cells.clear()
            self._await_cells_revision = self._tree_revision
        key = (from_id, to_id)
        if key not in self._await_cells:
            self._await_cells[key] = await_highlight_cells(self.tree_lines, from_id, to_id)
        return self._await_cells[key]

    def _tick_route_anims(self) -> None:
        """Draw every trace where it is now, then advance it one step.

        Drawing before advancing is what puts the first frame on the sender
        rather than one step past it. A trace that has run out of steps is
        dropped here without being drawn, so the tick that retires the last
        one is also the tick that clears the tree of it — an animation that
        ends by stopping its own timer would otherwise leave its final frame
        on screen for good.
        """
        self._reap_await_anims()
        overlays: dict[Key, LeafOverlay] = {}
        for await_anim in self._await_anims.values():
            for await_cell in self._await_route_cells(await_anim.from_id, await_anim.to_id) or ():
                col = await_cell.cell[1]
                leaf_index, row_in_leaf = cell_leaf(await_cell.cell)
                if not 0 <= leaf_index < len(self.tree_lines):
                    continue
                heavy = _await_route_glyph(await_cell.glyph, await_cell.directions)
                if heavy == await_cell.glyph:
                    # The route crosses this cell without using any of its
                    # arms; tinting it would light a rail the await never took.
                    continue
                key = self.tree_lines[leaf_index][2]
                overlays.setdefault(key, {})[(row_in_leaf, col)] = (
                    heavy,
                    _await_route_style(await_anim.frame, await_cell.offset),
                )
            await_anim.frame = (await_anim.frame + 1) % 10

        alive: list[RouteAnim] = []
        for route_anim in self._route_anims:
            path = send_path(self.tree_lines, route_anim.from_id, route_anim.to_id)
            if not path or route_anim.step >= len(path):
                continue
            cell = path[route_anim.step]
            leaf_index, row_in_leaf = cell_leaf(cell)
            if not 0 <= leaf_index < len(self.tree_lines):
                continue
            key = self.tree_lines[leaf_index][2]
            overlays.setdefault(key, {})[(row_in_leaf, cell[1])] = _send_trace_glyph(
                path, route_anim.step
            )
            route_anim.step += 1
            alive.append(route_anim)
        self._route_anims = alive
        panel = self._panel()
        if panel is not None:
            panel.set_overlays(overlays)
        if not self._route_anims and not self._await_anims:
            self._stop_anim_timer()

    def _stop_anim_timer(self) -> None:
        if self._anim_timer is not None:
            self._anim_timer.stop()
            self._anim_timer = None

    # ---- rendering -----------------------------------------------------

    def _panel(self) -> TreePanel | None:
        """The tree panel, or None when there is no mounted widget to draw on.

        Reactive watchers fire on assignment, which can happen before mount and
        during teardown as well as in the middle of a normal frame. A missing
        widget in those windows is expected, not an error.
        """
        try:
            return self.query_one("#tree-panel", TreePanel)
        except Exception:
            return None

    def _render_tree(self) -> None:
        panel = self._panel()
        if panel is None:
            return
        panel._lines_data = self.tree_lines
        panel.lines = self.tree_lines
        panel.apply_cursor(self.cursor, self.staged_pane)
        panel.scroll_to_cursor(self.cursor)

    def _index_for_key(self, key: Key) -> int | None:
        """The line index of *key* in the current tree, or None."""
        for i, (_, _, k, _, _) in enumerate(self.tree_lines):
            if k == key:
                return i
        return None

    def watch_cursor(self, cursor: int) -> None:
        """Redraw the tree with the cursor highlighted."""
        panel = self._panel()
        if panel is None:
            return
        panel.apply_cursor(cursor, self.staged_pane)
        panel.scroll_to_cursor(cursor)

    def watch_staged_pane(self, _pane: str | None) -> None:
        """Redraw the tree so the staged pane gets its background."""
        panel = self._panel()
        if panel is None:
            return
        panel.apply_cursor(self.cursor, self.staged_pane)

    def watch_bus_visible(self, visible: bool) -> None:
        """Show or hide the bus panel, giving the tree the space either way."""
        try:
            panel = self.query_one("#bus-panel", RichLog)
        except Exception:
            # Missing widget is expected before mount / during teardown (cf. _panel).
            return
        panel.set_class(not visible, "-hidden")

    # ---- actions -------------------------------------------------------

    def action_toggle_bus(self) -> None:
        """Show or hide the bus panel. Offered by the command palette."""
        self.bus_visible = not self.bus_visible

    def action_cursor_down(self) -> None:
        if self.cursor < len(self.tree_lines) - 1:
            self.cursor += 1
            self._render_tree()

    def action_cursor_up(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1
            self._render_tree()

    async def action_stage(self) -> None:
        """Stage the selected agent: join its pane into the régie's window.

        If something is already staged, break it back out to a hidden window
        first. Then join the new pane in and resize it to fill the stage area.
        """
        node = selected_participant(self.tree_lines, self.cursor)
        if node is None:
            self.notify("nothing to stage", severity="warning")
            return
        pane = node.get("tmux_pane")
        if not pane:
            self.notify(f"{node.get('id', '?')[:8]} has no pane", severity="warning")
            return
        if not self.my_window:
            self.notify("régie window not discovered — cannot stage", severity="error")
            return

        if self.staged_pane and self.staged_pane != pane:
            try:
                await panes.break_pane(self.staged_pane)
            except Exception as exc:
                logger.debug("unstage failed: %s", exc)

        if self.staged_pane == pane:
            # Toggle: unstaging the current occupant
            try:
                await panes.break_pane(pane)
                self.staged_pane = None
            except Exception as exc:
                self.notify(f"unstage failed: {exc}", severity="error")
            return

        try:
            await panes.join_pane(pane, target_window=self.my_window)
            self.staged_pane = pane
            # Resize the régie pane, not the staged one: tmux gives the rest
            # to the staged pane, and this avoids knowing the window's
            # column count.
            if self.my_pane:
                try:
                    await panes.resize_pane(self.my_pane, width=self.settings.regie.sidebar_width)
                except Exception as exc:
                    logger.debug("resize after stage failed: %s", exc)
        except Exception as exc:
            self.notify(f"stage failed: {exc}", severity="error")

    async def action_kill(self) -> None:
        """Kill the selected participant."""
        node = selected_participant(self.tree_lines, self.cursor)
        if node is None:
            self.notify("nothing to kill", severity="warning")
            return
        pid = node.get("id")
        if not pid:
            self.notify("cannot kill an unmanaged pane", severity="warning")
            return
        if not self._client:
            return
        try:
            await self._client.call("participant.kill", id=pid)
        except Exception as exc:
            self.notify(f"kill failed: {exc}", severity="error")
        await self._refresh_tree()

    def action_spawn(self) -> None:
        from textual.command import CommandPalette

        self.push_screen(
            CommandPalette(
                providers=[SpawnHarnessCommands],
                placeholder="Spawn a fresh session\u2026",
            ),
        )

    def spawn_harness(self, harness: str) -> None:
        """Start a bare CLI of this harness, from the command palette.

        Sync on purpose: a palette hit runs its command as a plain callback,
        and handing it a worker rather than a coroutine keeps the palette from
        having to care whether the daemon is slow to answer.
        """
        self.run_worker(self._spawn_harness(harness), exclusive=False)

    async def _spawn_harness(self, harness: str) -> None:
        if not self._client:
            return
        try:
            await self._client.call(
                "spawn",
                harness=harness,
                # No prompt, no parent: the palette starts a CLI, not a
                # delegation. `manual` adds no approval flags.
                prompt="",
                approval="manual",
                cwd=str(Path.cwd()),
                # By name, and only ours: a new window belongs in the
                # user's session, not the fallback `theater` one.
                tmux_session=self.my_session_name,
            )
        except Exception as exc:
            self.notify(f"spawn failed: {exc}", severity="error")
            return
        # The new agent appears in the tree on the next refresh.
        await self._refresh_tree()

    async def load_dead_sessions(self) -> list[dict]:
        if self._client is None:
            return []
        try:
            rows = await self._client.call("participants.recent_dead", limit=20)
            assert isinstance(rows, list)
        except Exception:
            return []
        return rows

    def action_resume_dead_session(self) -> None:
        from textual.command import CommandPalette

        self.push_screen(
            CommandPalette(
                providers=[ResumeDeadSessionCommands],
                placeholder="Search for dead sessions\u2026",
            ),
        )

    def resume_dead_session(self, row: dict) -> None:
        state = row.get("resume_state", "")
        if state != "resumable":
            reasons = {
                "live": "session is still running",
                "no_session_id": "no session id was recorded",
                "harness_cannot_resume": "this harness does not support resume",
                "untrusted": "transcript identity could not be verified",
                "owned_by_live": "another live session holds this session id",
            }
            reason = reasons.get(state, state or "unknown reason")
            self.notify(
                f"Cannot resume: {reason}",
                title="Session not resumable",
                severity="warning",
            )
            return
        if not row.get("cwd"):
            self.notify("Cannot resume: original cwd is missing", severity="warning")
            return
        self.run_worker(self._resume_dead_session(row), exclusive=False)

    async def _resume_dead_session(self, row: dict) -> None:
        if self._client is None:
            return
        try:
            await self._client.call(
                "spawn",
                harness=row["harness"],
                prompt="",
                cwd=row["cwd"],
                approval="manual",
                resume=row["session_id"],
                worktree=False,
                tmux_session=self.my_session_name,
            )
        except Exception as exc:
            self.notify(f"resume failed: {exc}", severity="error")
            return
        await self._refresh_tree()

    async def action_focus_stage(self) -> None:
        """Stage the selected agent if needed, then focus it. Bound to `l`.

        The way back is `<prefix> h`, not a régie key — see `_bind_return_key`.
        """
        node = selected_participant(self.tree_lines, self.cursor)
        pane = node.get("tmux_pane") if node else None
        if not (pane and pane == self.staged_pane):
            await self.action_stage()
            if self.staged_pane != pane:
                # Staging failed, or there was nothing to stage: action_stage
                # already notified why. Don't focus whatever was staged before.
                return
        if not self.staged_pane:
            return
        try:
            await panes.select_pane(self.staged_pane)
        except Exception as exc:
            self.notify(f"focus failed: {exc}", severity="error")

    # ---- tree rendering with cursor ------------------------------------


def run_regie(settings: Config | None = None) -> None:
    """Entry point for `theater regie`."""
    app = RegieApp(settings)
    app.run()
