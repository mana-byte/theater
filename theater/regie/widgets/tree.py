"""TreePanel and TreeStack: the scrollable participant tree and its overlay stack.

TreePanel reconciles by key rather than rebuilding on every refresh: a
widget that survives a tick is updated in place so per-widget state (a
hover class, an animation timer) is not destroyed. TreeStack clamps the
usage-breakdown overlay to the remaining height on resize.
"""

from __future__ import annotations

import contextlib
from collections.abc import Collection, Mapping

from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label

from theater.constants.regie import REGIE_EMPTY_TREE_KEY
from theater.regie.animations.routes import LeafOverlay
from theater.regie.render.layout import Key, is_root_prefix
from theater.regie.widgets.chrome import EmptyTreeState
from theater.regie.widgets.leaf import AgentLeaf
from theater.regie.widgets.usage_breakdown import UsageBreakdownPanel


def _is_participant_key(key: Key) -> bool:
    """Whether *key* identifies a participant or unmanaged pane (not a separator)."""
    return key[0] in ("p", "u")


class TreePanel(VerticalScroll):
    """A scrollable list of participant tree leaves.

    Each participant is an :class:`AgentLeaf` widget spanning three rows.
    Separator rows stay plain ``Label`` widgets; the empty state is its own
    full-panel widget. The panel scrolls natively when the list is longer than
    the viewport. Cursor and staged highlighting are done via Textual CSS
    classes (which respect the user's theme) rather than hardcoded Rich colours.

    Reconciliation preserves surviving widgets. Retiring leaves remain mounted
    at their prior slots but leave the active-key map immediately.
    """

    can_focus = False

    lines: reactive[list[tuple[Content, dict, Key, str, str]]] = reactive([])

    #: Key for the placeholder shown when the tree is empty.
    _EMPTY_KEY: Key = REGIE_EMPTY_TREE_KEY

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        #: Set by the app so apply_cursor can map a line index back to its node.
        self._lines_data: list[tuple[Content, dict, Key, str, str]] = []
        #: Stable widget map, keyed by the row key from render_tree.
        self._key_widgets: dict[Key, Widget] = {}
        #: Which leaves currently carry a send trace, so the next frame knows which to clear.
        self._overlaid: set[Key] = set()
        #: Startup reveal widths by initially-visible key; absent keys render fully.
        self._reveals: dict[Key, int] = {}
        #: Leaves excluded from active tree keys while their reverse reveal finishes.
        self._retiring: dict[Key, AgentLeaf] = {}
        #: Previous row keys that anchor retiring leaves in their former order.
        self._retiring_predecessors: dict[Key, Key | None] = {}

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

        Existing widgets update in place; new rows mount by key. Retiring
        leaves stay in their saved slots until their reverse reveal completes.
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
        ordered_rows: list[tuple[Key, Widget]] = []
        participant_index = 0
        for i, (label, node, key, prefix, cont_prefix) in enumerate(lines):
            widget = self._reconcile_row(label, node, key, prefix, cont_prefix, cwd_segments, i)
            if _is_participant_key(key):
                widget.set_class(participant_index % 2 == 0, "tree-alt")
                participant_index += 1
            ordered_rows.append((key, widget))

        ordered_widgets = self._merge_retiring(ordered_rows)
        for i, widget in enumerate(ordered_widgets):
            if i == 0:
                continue
            self.move_child(widget, after=ordered_widgets[i - 1])

    def _merge_retiring(self, rows: list[tuple[Key, Widget]]) -> list[Widget]:
        """Insert tombstones after their saved predecessors without reordering them."""
        groups: dict[Key | None, list[tuple[Key, AgentLeaf]]] = {}
        for retired_key, retired_widget in self._retiring.items():
            groups.setdefault(self._retiring_predecessors[retired_key], []).append(
                (retired_key, retired_widget)
            )

        ordered: list[Widget] = []

        def append_retirees(predecessor: Key | None) -> None:
            for retired_key, retired_widget in groups.get(predecessor, []):
                ordered.append(retired_widget)
                append_retirees(retired_key)

        append_retirees(None)
        for row_key, row_widget in rows:
            ordered.append(row_widget)
            append_retirees(row_key)
        return ordered

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
                reveal=self._reveals.get(key),
            )
        else:
            widget = Label(label)
        self._key_widgets[key] = widget
        self.mount(widget)
        return widget

    def _reconcile_empty(self) -> None:
        """Show the empty-tree call to action, removing all row widgets."""
        for key in list(self._key_widgets):
            if key != self._EMPTY_KEY:
                self._remove_widget(self._key_widgets.pop(key))
        if self._retiring:
            return
        if self._EMPTY_KEY not in self._key_widgets:
            widget = EmptyTreeState(reveal=self._reveals.get(self._EMPTY_KEY))
            self._key_widgets[self._EMPTY_KEY] = widget
            self.mount(widget)

    def leaf_keys(self) -> tuple[Key, ...]:
        """Return current leaf keys without rendering their content."""
        return tuple(
            key
            for key, widget in self._key_widgets.items()
            if isinstance(widget, AgentLeaf | EmptyTreeState)
        )

    def reveal_widths(self, keys: Collection[Key]) -> dict[Key, int]:
        """Return full row widths for the requested reveal keys."""
        selected = set(keys)
        widths: dict[Key, int] = {}
        for key, widget in self._key_widgets.items():
            if key in selected and isinstance(widget, AgentLeaf | EmptyTreeState):
                widths[key] = widget.required_reveal_width
        return widths

    def set_reveals(self, reveals: dict[Key, int]) -> None:
        """Apply one complete startup frame by stable tree key."""
        self._reveals = dict(reveals)
        for key, widget in self._key_widgets.items():
            reveal = self._reveals.get(key)
            if isinstance(widget, AgentLeaf | EmptyTreeState):
                widget.set_reveal(reveal)

    def retire(self, keys: Collection[Key]) -> dict[Key, int]:
        """Turn active leaves into non-addressable tombstones and return their widths."""
        candidates = set(keys)
        retiring: list[tuple[Key, Key | None]] = []
        previous: Key | None = None
        for _, _, key, _, _ in self._lines_data:
            if key in candidates:
                retiring.append((key, previous))
            previous = key
        seen = {key for key, _ in retiring}
        retiring.extend((key, None) for key in candidates - seen)

        widths: dict[Key, int] = {}
        for key, predecessor in retiring:
            widget = self._key_widgets.pop(key, None)
            if not isinstance(widget, AgentLeaf):
                continue
            self._reveals.pop(key, None)
            self._overlaid.discard(key)
            widget.retire()
            self._retiring[key] = widget
            self._retiring_predecessors[key] = predecessor
            widths[key] = widget.visible_reveal_width
        return widths

    def restore_retiring(self, keys: Collection[Key]) -> None:
        """Return leaves that reappeared before their retirement completed."""
        for key in keys:
            widget = self._retiring.pop(key, None)
            if widget is not None:
                self._retiring_predecessors.pop(key, None)
                widget.set_reveal(None)
                self._key_widgets[key] = widget

    def set_retiring_reveals(self, reveals: Mapping[Key, int]) -> None:
        """Apply a reverse-reveal frame to leaves outside the active key map."""
        for key, reveal in reveals.items():
            widget = self._retiring.get(key)
            if widget is not None:
                widget.set_reveal(reveal)

    def finish_retiring(self, keys: Collection[Key]) -> None:
        """Unmount tombstones whose reverse reveal reached zero columns."""
        for key in keys:
            widget = self._retiring.pop(key, None)
            if widget is not None:
                self._retiring_predecessors.pop(key, None)
                self._remove_widget(widget)
        if not self._retiring and not self._lines_data:
            self._reconcile_empty()

    def _remove_widget(self, widget: Widget) -> None:
        """Remove a widget via Textual's public API.

        Pending Textual removals never enter the active key map.
        """
        widget.remove()

    def apply_cursor(
        self,
        cursor: int,
        staged_pane: str | None,
        staged_participant_id: str | None = None,
    ) -> None:
        """Add CSS classes to the cursor and staged lines, remove from others.

        Active keys, rather than child positions, own highlighting.
        """
        for i, (_, node, key, _, _) in enumerate(self._lines_data):
            widget = self._key_widgets.get(key)
            if widget is None:
                continue
            widget.remove_class("tree-cursor")
            widget.remove_class("tree-staged")
            pane_matches = staged_pane is not None and node.get("tmux_pane") == staged_pane
            participant_matches = (
                staged_pane is None
                and staged_participant_id is not None
                and key[0] == "p"
                and node.get("id") == staged_participant_id
            )
            if pane_matches or participant_matches:
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


class TreeStack(Vertical):
    """Tree plus the usage overlay, clamped to the remaining height."""

    def on_resize(self, event: events.Resize) -> None:
        # Use the event size: reading stack.size here lags one resize.
        with contextlib.suppress(Exception):
            panel = self.query_one(UsageBreakdownPanel)
            if panel.has_class("-visible"):
                panel.constrain_to_height(event.size.height)
