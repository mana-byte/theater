"""Reconciled, animated participant tree backed by public state."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Literal, cast

from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label

from regie.marks import MarkKind, RowMark
from regie.menu import ROOT_ID, MenuItem, MenuView, menu_nodes
from regie.motions.retirement import LeafRetirementController
from regie.motions.reveal import LeafRevealController
from regie.motions.routes import AwaitMarks, LeafOverlay
from regie.motions.wipe import LeafWipeController
from regie.render.layout import Key, RenderedLine, TreeLines, is_root_prefix, render_tree
from regie.render.routing import RailArms, heavy_overlays, section_arms
from regie.tree import tree_for_projection
from regie.tree_layout import TreeLayout
from regie.tree_nodes import RawRenderNode, RenderNode, TreeNode
from regie.ui_constants import (
    REGIE_EMPTY_TREE_KEY,
    REGIE_MARK_SEEN_SECONDS,
)
from regie.widgets.chrome import EmptyTreeState
from regie.widgets.leaf import AgentLeaf
from regie.widgets.menu_row import MenuRow
from regie.widgets.separator import SeparatorRow
from regie.widgets.spawn_leaf import SpawnLeaf
from regie.widgets.tree_parts.motion import TreeMotion
from regie.widgets.usage_breakdown import UsageBreakdownPanel
from theater.frontend import StateProjection


def _is_participant_key(key: Key) -> bool:
    """Whether a row is an interactive managed or unmanaged leaf."""
    return key[0] in {"p", "u"}


def _is_managed_key(key: Key) -> bool:
    return key[0] == "p"


def _is_selectable_key(key: Key) -> bool:
    return key[0] in {"p", "s", "u", "g", "m"}


def _is_menu_key(key: Key | None) -> bool:
    """Whether a row belongs to the options menu closing the tree."""
    return key is not None and key[0] in {"g", "m"}


def _hidden_owners(nodes: list[TreeNode]) -> dict[Key, Key]:
    """Map each participant folded out of sight to the row that stands for it on screen."""
    owners: dict[Key, Key] = {}
    pending = list(nodes)
    while pending:
        node = pending.pop()
        for hidden in cast(RawRenderNode, node).get("hidden_ids", []):
            owners[("p", hidden)] = ("p", node["id"])
        pending.extend(node["children"])
    return owners


class ParticipantTree(TreeMotion, VerticalScroll):
    """Keep one interactive leaf per stable public participant ID."""

    class MenuClicked(Message):
        """A menu row was clicked and the cursor is on it: the app picks it."""

        def __init__(self, key: Key) -> None:
            super().__init__()
            self.key = key

    class MarkSeen(Message):
        """The cursor rested on a row long enough that its failure counts as seen."""

        def __init__(self, operation_id: str) -> None:
            super().__init__()
            self.operation_id = operation_id

    can_focus = False
    _EMPTY_KEY: Key = REGIE_EMPTY_TREE_KEY

    DEFAULT_CSS = """
    ParticipantTree {
        height: 1fr;
        scrollbar-size: 0 0;
    }
    ParticipantTree > Label {
        height: 1;
        padding: 0 2;
        margin: 0 0;
    }
    ParticipantTree > AgentLeaf.tree-alt {
        background: $foreground 3%;
    }
    ParticipantTree > AgentLeaf.tree-alt:hover {
        background: $accent 10%;
    }
    ParticipantTree > AgentLeaf.tree-alt.tree-cursor {
        background: $accent 20%;
        text-style: bold;
    }
    """

    def __init__(self, *args, startup_reveal: bool = True, motion: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lines_data: list[RenderedLine] = []
        self._key_widgets: dict[Key, Widget] = {}
        self._overlaid: set[Key] = set()
        self._reveals: dict[Key, int] = {}
        self._retiring: dict[Key, AgentLeaf] = {}
        self._retiring_predecessors: dict[Key, Key | None] = {}
        self._selected_key: Key | None = None
        self._hovered_separator: Key | None = None
        self._staged_id: str | None = None
        self._staged_unmanaged_id: str | None = None
        self._trajectory_id: str | None = None
        self._participant_detail: Literal["cwd", "description"] = "cwd"
        self._cwd_segments = 2
        self._participant_costs: dict[str, int] = {}
        self._stale = False
        self._cursor_visible = True
        self._revision = 0
        self._animate_new: set[Key] = set()
        self._leaf_reveal = LeafRevealController(enabled=startup_reveal and motion)
        self._leaf_retirement = LeafRetirementController(enabled=startup_reveal and motion)
        self._leaf_reveal_timer: Timer | None = None
        self._leaf_retirement_timer: Timer | None = None
        self._motion = motion
        self._wipes = LeafWipeController(enabled=motion)
        self._wipe_timer: Timer | None = None
        self._statuses: dict[Key, str] = {}
        self._attention_since: dict[Key, float] = {}
        self._attention_phase = 0
        self._attention_timer: Timer | None = None
        self._await_marks = AwaitMarks()
        self._mark_kinds: dict[str, MarkKind] | None = None
        #: A folded-away participant's key, and the visible row that stands for it.
        self._hidden_owner: dict[Key, Key] = {}
        self._seen_timer: Timer | None = None
        self._seen_pending: str | None = None
        #: Rows asked to be killed, and rows gone from the projection that stay on screen, widget
        #: and width kept, until the sweep still running on them ends: ``key -> (animate, width)``.
        self._kills: set[Key] = set()
        self._held: dict[Key, tuple[bool, int]] = {}
        #: The options menu: what it holds (set by the app), what the human has opened, the row
        #: the cursor left to enter it, and the inputs it is redrawn from without a projection.
        self.menu_source: Callable[[], Sequence[MenuItem]] = lambda: ()
        self._menu_view = MenuView()
        self._menu_return: Key | None = None
        self._menu_hidden = False
        self._menu_built_open = False
        self._tree_nodes: list[TreeNode] = []
        self._unmanaged: list[dict] | None = None

    @property
    def selected_id(self) -> str | None:
        return None if self._selected_key is None else self._selected_key[1]

    @property
    def selected_key(self) -> Key | None:
        return self._selected_key

    @property
    def selected_participant_id(self) -> str | None:
        key = self._selected_key
        return key[1] if key is not None and _is_managed_key(key) else None

    @property
    def selected_unmanaged_pane(self) -> str | None:
        key = self._selected_key
        return key[1] if key is not None and key[0] == "u" else None

    @property
    def participant_ids(self) -> tuple[str, ...]:
        return tuple(key[1] for _, _, key, _, _ in self._lines_data if _is_managed_key(key))

    @property
    def selectable_keys(self) -> tuple[Key, ...]:
        return tuple(key for _, _, key, _, _ in self._lines_data if _is_selectable_key(key))

    @property
    def tree_lines(self) -> list[RenderedLine]:
        return self._lines_data

    def stand_in(self, participant_id: str) -> str | None:
        """The row that shows *participant_id*: itself, the folded agent hiding it, or None."""
        if ("p", participant_id) in self._key_widgets:
            return participant_id
        owner = self._hidden_owner.get(("p", participant_id))
        return None if owner is None else owner[1]

    @property
    def revision(self) -> int:
        return self._revision

    def show_projection(
        self,
        projection: StateProjection,
        *,
        participant_detail: str = "cwd",
        cwd_segments: int = 2,
        stage_reasons: Mapping[str, str] | None = None,
        harness_icons: Mapping[str, str] | None = None,
        selected_id: str | None = None,
        staged_id: str | None = None,
        staged_unmanaged_id: str | None = None,
        trajectory_id: str | None = None,
        unmanaged: list[dict] | None = None,
        layout: TreeLayout | None = None,
        pending_spawn: Mapping[str, object] | None = None,
        marks: Mapping[str, RowMark] | None = None,
    ) -> str | None:
        old_keys = self.selectable_keys
        try:
            old_index = old_keys.index(self._selected_key) if self._selected_key is not None else 0
        except ValueError:
            old_index = 0
        tree = tree_for_projection(
            projection,
            harness_icons=harness_icons,
            participant_costs=self._participant_costs,
            layout=layout,
            pending_spawn=pending_spawn,
            marks=marks,
        )
        self._hidden_owner = _hidden_owners(tree)
        reasons = stage_reasons or {}
        self._add_stage_reasons(tree, reasons)
        self._tree_nodes, self._unmanaged = tree, unmanaged
        self._menu_hidden = pending_spawn is not None  # the new agent's row is where to look
        self._participant_detail = "description" if participant_detail == "description" else "cwd"
        self._cwd_segments = cwd_segments
        lines = self._render_lines()
        self._observe_statuses(projection)
        self._observe_marks(marks or {})
        self._sync_retirement(projection)
        self._lines_data = lines
        self._revision += 1
        self._stale = projection.stale
        self._reconcile(lines)
        self._settle_selection(selected_id, old_index)
        if self._in_menu() != self._menu_built_open:  # the settled cursor entered or left the menu
            self._rerender()
        self._staged_id = staged_id
        self._staged_unmanaged_id = staged_unmanaged_id
        self._trajectory_id = trajectory_id
        self._apply_selection()
        self._animate_new = {
            ("p", participant.participant_id)
            for participant in projection.participants.values()
            if participant.parent_id
        }
        self._sync_reveal()
        self._sync_attention()
        self._apply_await_marks()
        self._sync_mark_seen()
        return self.selected_participant_id

    def set_usage_costs(self, costs: Mapping[str, int]) -> None:
        self._participant_costs = dict(costs)
        for _, node, key, _, _ in self._lines_data:
            if not _is_managed_key(key):
                continue
            participant_id = key[1]
            cost = costs.get(participant_id)
            widget = self._key_widgets.get(key)
            if isinstance(widget, AgentLeaf):
                widget.set_usage_cost(cost)
            else:
                cast(RawRenderNode, node)["usage_cost_microcents"] = cost

    @staticmethod
    def _add_stage_reasons(nodes: list[TreeNode], reasons: Mapping[str, str]) -> None:
        pending = list(nodes)
        while pending:
            node = pending.pop()
            if node["id"] in reasons:
                node["stage_reason"] = reasons[node["id"]]
            pending.extend(node["children"])

    def select(self, participant_id: str | None) -> str | None:
        key = ("p", participant_id or "")
        if key in self.selectable_keys:
            self._set_selected(key)
            self._apply_selection()
        return self.selected_participant_id

    def select_key(self, key: Key) -> str | None:
        if key in self.selectable_keys:
            self._set_selected(key)
            self._apply_selection()
        return self.selected_id

    def move(self, offset: int) -> str | None:
        keys = self.selectable_keys
        if not keys:
            self._selected_key = None
            return None
        try:
            index = keys.index(self._selected_key) if self._selected_key is not None else 0
        except ValueError:
            index = 0
        self._set_selected(keys[max(0, min(len(keys) - 1, index + offset))])
        self._apply_selection()
        return self.selected_id

    def mark_surfaces(
        self,
        *,
        staged_id: str | None,
        staged_unmanaged_id: str | None = None,
        trajectory_id: str | None,
    ) -> None:
        self._staged_id = staged_id
        self._staged_unmanaged_id = staged_unmanaged_id
        self._trajectory_id = trajectory_id
        self._apply_selection()

    def set_cursor_visible(self, visible: bool) -> None:
        self._cursor_visible = visible
        self._apply_selection()

    def _in_menu(self) -> bool:
        return _is_menu_key(self._selected_key)

    @property
    def in_menu(self) -> bool:
        """Whether the cursor is on the options menu (the root, an option or a choice)."""
        return self._in_menu()

    @property
    def menu_return(self) -> Key | None:
        """The row the cursor was on before it entered the menu."""
        return self._menu_return

    @property
    def menu_view(self) -> MenuView:
        return self._menu_view

    def _render_lines(self) -> TreeLines:
        """The agents as rows, then the menu as one more root, open while the cursor is in it."""
        opened = self._in_menu()
        self._menu_built_open = opened
        menu = None
        if not self._menu_hidden and (self._tree_nodes or opened):
            menu = menu_nodes(self.menu_source(), self._menu_view, open=opened)
        return render_tree(
            self._tree_nodes,
            unmanaged=self._unmanaged,
            cwd_segments=self._cwd_segments,
            menu=menu,
        )

    def _rerender(self) -> None:
        """Draw the rows again from what the tree knows: the menu opened, closed or changed."""
        lines = self._render_lines()
        self._lines_data = lines
        self._revision += 1
        self._reconcile(lines)
        self._settle_selection(None, 0)  # a choice may have gone with the list that held it
        self._apply_selection()

    def _set_selected(self, key: Key | None) -> None:
        """Move the cursor, opening the menu as it enters and closing it as it leaves."""
        entered, left = (
            not self._in_menu() and _is_menu_key(key),
            self._in_menu() and not _is_menu_key(key),
        )
        if entered:
            self._menu_return = self._selected_key
        if left:
            self._menu_view, self._menu_return = MenuView(), None
        self._selected_key = key
        if entered or left:
            self._rerender()

    def _settle_selection(self, selected_id: str | None, old_index: int) -> None:
        """Keep the cursor on a row that exists; never fall into the menu when an agent remains."""
        keys = self.selectable_keys
        if self._selected_key in keys:
            return
        preferred = ("p", selected_id) if selected_id is not None else None
        rows = [key for key in keys if not _is_menu_key(key)] or list(keys)
        if preferred in keys:
            self._selected_key = preferred
        elif _is_menu_key(self._selected_key) and any(_is_menu_key(key) for key in keys):
            self._selected_key = ("g", ROOT_ID)  # an option went away: back to the menu's root
        else:
            self._selected_key = rows[min(old_index, len(rows) - 1)] if rows else None

    def open_menu(self, option: str | None = None) -> None:
        """Put the cursor in the menu: on its root, or on the first choice of *option*, unfolded."""
        if self._menu_hidden:  # a new agent is asking for its directory
            return
        if option is not None:
            self._menu_view = replace(self._menu_view, expanded=self._menu_view.expanded | {option})
        self._set_selected(("g", ROOT_ID))
        if option is not None:
            self._rerender()
            first = next(
                (key for key in self.selectable_keys if key[1].startswith(f"{option}/")), None
            )
            self._set_selected(first or ("m", option))
        self._apply_selection()

    def close_menu(self) -> Key | None:
        """Take the cursor back to the row it left, or the nearest agent if that row is gone."""
        back = self._menu_return
        rows = [key for key in self.selectable_keys if not _is_menu_key(key)]
        if back not in rows:
            back = rows[-1] if rows else None
        self._set_selected(back)
        self._apply_selection()
        return back

    def menu_root(self) -> MenuRow | None:
        """The ``+ options`` row, where the filter is typed."""
        root = self._key_widgets.get(("g", ROOT_ID))
        return root if isinstance(root, MenuRow) else None

    def set_menu_view(self, view: MenuView) -> None:
        """Change what is opened in the menu (an option unfolded, a list shown whole, a filter)."""
        if view != self._menu_view:
            self._menu_view = view
            if self._in_menu():
                self._rerender()

    def refresh_menu(self) -> None:
        """Redraw the menu after what it holds changed (a list finished loading, a theme picked)."""
        if self._in_menu():
            self._rerender()

    def on_menu_row_clicked(self, message: MenuRow.Clicked) -> None:
        message.stop()
        self.select_key(message.key)
        self.post_message(self.MenuClicked(message.key))

    def _reconcile(self, lines: list[RenderedLine]) -> None:
        if not lines:
            self._reconcile_empty()
            return
        new_keys = {key for _, _, key, _, _ in lines}
        if self._EMPTY_KEY in self._key_widgets:
            self._remove_widget(self._key_widgets.pop(self._EMPTY_KEY))
        for key in list(self._key_widgets):
            if key not in new_keys:
                self._remove_widget(self._key_widgets.pop(key))

        ordered_rows: list[tuple[Key, Widget]] = []
        participant_index = 0
        for index, (label, node, key, prefix, cont_prefix) in enumerate(lines):
            widget = self._reconcile_row(label, node, key, prefix, cont_prefix, index)
            if _is_participant_key(key):
                widget.set_class(participant_index % 2 == 0, "tree-alt")
                participant_index += 1
            ordered_rows.append((key, widget))

        ordered = self._merge_retiring(ordered_rows)
        for index, widget in enumerate(ordered):
            if index:
                self.move_child(widget, after=ordered[index - 1])

    def _reconcile_row(
        self,
        label: Content,
        node: RenderNode,
        key: Key,
        prefix: str,
        cont_prefix: str,
        index: int,
    ) -> Widget:
        first_root = index == 0 and is_root_prefix(prefix)
        widget = self._key_widgets.get(key)
        if isinstance(widget, AgentLeaf):
            widget.update_node(
                node,
                prefix=prefix,
                cont_prefix=cont_prefix,
                participant_detail=self._participant_detail,
                is_first_root=first_root,
            )
            return widget
        if isinstance(widget, SeparatorRow):
            widget.update_node(node, prefix, is_first_root=first_root)
            return widget
        if isinstance(widget, SpawnLeaf):
            widget.update_node(node, prefix, cont_prefix=cont_prefix)
            return widget
        if isinstance(widget, MenuRow):
            widget.update_node(node, prefix, cont_prefix=cont_prefix, is_first_root=first_root)
            return widget
        if isinstance(widget, Label):
            widget.update(label)
            return widget
        if key[0] in {"g", "m"}:
            widget = MenuRow(
                node, prefix, cont_prefix=cont_prefix, key=key, is_first_root=first_root
            )
        elif key[0] == "s":
            widget = SeparatorRow(node, prefix, key=key, is_first_root=first_root)
        elif key[0] == "n":
            widget = SpawnLeaf(node, prefix, cont_prefix=cont_prefix, key=key)
            self._wipes.start(key, "$secondary")  # the row that asks for the directory opens
            self._ensure_wipe_timer()
        elif _is_participant_key(key):
            widget = AgentLeaf(
                node,
                prefix,
                cont_prefix=cont_prefix,
                key=key,
                cwd_segments=self._cwd_segments,
                participant_detail=self._participant_detail,
                is_first_root=first_root,
                reveal=self._reveals.get(key),
            )
        else:
            widget = Label(label)
        self._key_widgets[key] = widget
        self.mount(widget)
        return widget

    def _reconcile_empty(self) -> None:
        for key in list(self._key_widgets):
            if key != self._EMPTY_KEY:
                self._remove_widget(self._key_widgets.pop(key))
        if self._retiring:
            return
        if self._EMPTY_KEY not in self._key_widgets:
            widget = EmptyTreeState(reveal=self._reveals.get(self._EMPTY_KEY))
            self._key_widgets[self._EMPTY_KEY] = widget
            self.mount(widget)

    def _merge_retiring(self, rows: list[tuple[Key, Widget]]) -> list[Widget]:
        groups: dict[Key | None, list[tuple[Key, AgentLeaf]]] = {}
        for key, retiring_widget in self._retiring.items():
            groups.setdefault(self._retiring_predecessors[key], []).append((key, retiring_widget))
        ordered: list[Widget] = []

        def append_retirees(predecessor: Key | None) -> None:
            for key, widget in groups.get(predecessor, []):
                ordered.append(widget)
                append_retirees(key)

        append_retirees(None)
        for key, row_widget in rows:
            ordered.append(row_widget)
            append_retirees(key)
        return ordered

    def _apply_selection(self) -> None:
        for _, node, key, _, _ in self._lines_data:
            widget = self._key_widgets.get(key)
            if isinstance(widget, SeparatorRow):
                folded = widget.folded_ids
                widget.set_stage_marker(
                    "tmux"
                    if self._staged_id in folded
                    else "trajectory"
                    if self._trajectory_id in folded
                    else None
                )
                widget.set_cursor(self._cursor_visible and key == self._selected_key)
                continue
            if isinstance(widget, MenuRow):
                widget.set_cursor(self._cursor_visible and key == self._selected_key)
                continue
            if not isinstance(widget, AgentLeaf):
                continue
            participant_id = node.get("id")
            managed = _is_managed_key(key)
            under = cast(RawRenderNode, node).get("hidden_ids") or ()  # folded under this row
            staged = (
                managed and (participant_id == self._staged_id or self._staged_id in under)
            ) or (not managed and key[1] == self._staged_unmanaged_id)
            trajectory = (
                managed
                and (participant_id == self._trajectory_id or self._trajectory_id in under)
                and not staged
            )
            widget.set_class(staged, "tree-staged")
            widget.set_class(trajectory, "tree-trajectory-staged")
            widget.set_stage_marker("tmux" if staged else "trajectory" if trajectory else None)
            widget.set_cursor(self._cursor_visible and key == self._selected_key)
        self._apply_branch_highlight()
        self.scroll_to_selection()
        self._sync_mark_seen()

    def on_separator_row_hovered(self, message: SeparatorRow.Hovered) -> None:
        message.stop()
        if message.hovered:
            self._hovered_separator = message.key
        elif self._hovered_separator == message.key:
            self._hovered_separator = None
        self._apply_branch_highlight()

    def _apply_branch_highlight(self) -> None:
        """Heavy rails from a hovered or selected separator down to the agents it can fold."""
        selected = self._selected_key if self._cursor_visible else None
        arms: RailArms = {}
        for separator in {self._hovered_separator, selected}:
            if separator is None or separator[0] != "s":
                continue
            for key, cells in section_arms(self._lines_data, separator).items():
                row = arms.setdefault(key, {})
                for cell, wanted in cells.items():
                    row[cell] = row.get(cell, frozenset()) | wanted
        overlays = heavy_overlays(self._lines_data, arms)
        for key, widget in self._key_widgets.items():
            if isinstance(widget, (AgentLeaf, SeparatorRow)):
                widget.set_highlight(overlays.get(key))

    def scroll_to_selection(self) -> None:
        key = self._selected_key
        if key is None:
            return
        widget = self._key_widgets.get(key)
        if widget is not None:
            with contextlib.suppress(Exception):
                self.scroll_to_widget(widget)

    def set_overlays(self, overlays: dict[Key, LeafOverlay]) -> None:
        for key in self._overlaid - set(overlays):
            widget = self._key_widgets.get(key)
            if isinstance(widget, AgentLeaf | SeparatorRow):
                widget.set_overlay(None)
        for key, cells in overlays.items():
            widget = self._key_widgets.get(key)
            if isinstance(widget, AgentLeaf | SeparatorRow):
                widget.set_overlay(cells)
        self._overlaid = set(overlays)

    def attention_keys(self) -> tuple[Key, ...]:
        """Rows that want the human, top to bottom: waiting, dead or failed agents, and folds
        hiding one."""
        wanted = {"awaiting_input", "dead"}
        keys: list[Key] = []
        for _, node, key, _, _ in self._lines_data:
            mark = node.get("mark")
            if key[0] == "s":
                needs = node.get("folded_status") in wanted
            else:
                needs = _is_managed_key(key) and (
                    node.get("status") in wanted
                    or node.get("hidden_status") in wanted
                    or (isinstance(mark, RowMark) and mark.attention)
                )
            if needs:
                keys.append(key)
        return tuple(keys)

    def jump_attention(self, direction: int) -> str | None:
        """Move the cursor to the next (1) or previous (-1) row that wants the human, wrapping
        round; None when nothing does."""
        keys = self.attention_keys()
        if not keys:
            return None
        order = [key for _, _, key, _, _ in self._lines_data]
        here = order.index(self._selected_key) if self._selected_key in order else -1
        positions = sorted(order.index(key) for key in keys)
        ahead = [p for p in positions if (p > here if direction > 0 else p < here)]
        target = (
            ahead[0 if direction > 0 else -1] if ahead else positions[0 if direction > 0 else -1]
        )
        self.select_key(order[target])
        return self.selected_id

    def _sync_mark_seen(self) -> None:
        """Once the cursor has rested on a failure for a moment, report it seen."""
        wanted = self._seen_wanted()
        if wanted == self._seen_pending:
            return
        self._stop_mark_seen()
        if wanted is not None:
            self._seen_pending = wanted
            self._seen_timer = self.set_timer(
                REGIE_MARK_SEEN_SECONDS, lambda: self._report_seen(wanted)
            )

    def _seen_wanted(self) -> str | None:
        """The operation the visible cursor rests on that still wants the human, if any."""
        if not self._cursor_visible:  # the usage footer hides the row's cursor
            return None
        key = self._selected_key
        mark = next(
            (
                node.get("mark")
                for _, node, line_key, _, _ in self._lines_data
                if line_key == key and _is_managed_key(line_key)
            ),
            None,
        )
        return mark.operation_id if isinstance(mark, RowMark) and mark.attention else None

    def _report_seen(self, operation_id: str) -> None:
        self._seen_timer = None
        self._seen_pending = None
        if self._seen_wanted() == operation_id:
            self.post_message(self.MarkSeen(operation_id))

    def _stop_mark_seen(self) -> None:
        if self._seen_timer is not None:
            self._seen_timer.stop()
            self._seen_timer = None
        self._seen_pending = None

    def set_await_marks(self, marks: AwaitMarks) -> None:
        """Show who is blocked on a peer and who is being waited on."""
        if marks != self._await_marks:
            self._await_marks = marks
            self._apply_await_marks()

    def _apply_await_marks(self) -> None:
        marks = self._await_marks
        for key, widget in self._key_widgets.items():
            if isinstance(widget, AgentLeaf) and _is_managed_key(key):
                widget.set_await_marks(marks.waiting.get(key[1]), marks.awaited.get(key[1], 0))

    def _remove_widget(self, widget: Widget) -> None:
        widget.remove()

    def on_unmount(self) -> None:
        self._stop_motion()
        self._stop_mark_seen()


class TreeStack(Vertical):
    """Tree surface with a height-clamped usage overlay."""

    def on_resize(self, event: events.Resize) -> None:
        with contextlib.suppress(Exception):
            panel = self.query_one(UsageBreakdownPanel)
            if panel.has_class("-visible"):
                panel.constrain_to_height(event.size.height)
        sync = getattr(self.app, "_sync_ambience", None)
        if callable(sync):
            sync()


__all__ = ["ParticipantTree", "TreeStack", "_is_participant_key"]
