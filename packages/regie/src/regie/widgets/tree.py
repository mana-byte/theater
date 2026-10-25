"""Reconciled, animated participant tree backed by public state."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Collection, Mapping
from typing import Literal, cast

from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label

from regie.marks import MarkKind, RowMark
from regie.motions.retirement import LeafRetirementController, LeafRetirementFrame
from regie.motions.reveal import LeafRevealController
from regie.motions.routes import AwaitMarks, LeafOverlay
from regie.motions.wipe import LeafWipeController, status_wipe_color
from regie.render.layout import Key, RenderedLine, is_root_prefix, render_tree
from regie.render.routing import RailArms, heavy_overlays, section_arms
from regie.tree import tree_for_projection
from regie.tree_layout import TreeLayout
from regie.tree_nodes import RawRenderNode, RenderNode, TreeNode
from regie.ui_constants import (
    REGIE_ATTENTION_PULSE_INTERVAL,
    REGIE_ATTENTION_PULSE_LIMIT,
    REGIE_EMPTY_TREE_KEY,
    REGIE_MARK_SEEN_SECONDS,
    REGIE_STARTUP_REVEAL_INTERVAL_SECONDS,
    REGIE_WIPE_DEATH_SECONDS,
    REGIE_WIPE_INTERVAL_SECONDS,
)
from regie.widgets.chrome import EmptyTreeState
from regie.widgets.leaf import AgentLeaf
from regie.widgets.separator import SeparatorRow
from regie.widgets.spawn_leaf import SpawnLeaf
from regie.widgets.usage_breakdown import UsageBreakdownPanel
from theater.frontend import StateProjection


def _is_participant_key(key: Key) -> bool:
    """Whether a row is an interactive managed or unmanaged leaf."""
    return key[0] in {"p", "u"}


def _is_managed_key(key: Key) -> bool:
    return key[0] == "p"


def _is_selectable_key(key: Key) -> bool:
    return key[0] in {"p", "s", "u"}


#: The sweep that announces a mark arriving on a row.
_MARK_WIPES: dict[MarkKind, tuple[str, float | None]] = {
    MarkKind.TERMINATING: ("$error", REGIE_WIPE_DEATH_SECONDS),
    MarkKind.SENT: ("$success", None),
    MarkKind.BLOCKED: ("$warning", None),
    MarkKind.UNCERTAIN: ("$warning", None),
    MarkKind.FAILED: ("$error", None),
}


class ParticipantTree(VerticalScroll):
    """Keep one interactive leaf per stable public participant ID."""

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

    #: Breathing steps of the attention bar (dim, steady, bold), in order.
    _ATTENTION_BREATH = (-1, 0, 1, 0)

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
        self._seen_timer: Timer | None = None
        self._seen_pending: str | None = None
        #: Rows asked to be killed, and rows gone from the projection that stay on screen, widget
        #: and width kept, until the sweep still running on them ends: ``key -> (animate, width)``.
        self._kills: set[Key] = set()
        self._held: dict[Key, tuple[bool, int]] = {}

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

    @property
    def revision(self) -> int:
        return self._revision

    def configure(self, *, startup_reveal: bool) -> None:
        if self._leaf_reveal.started:
            return
        self._leaf_reveal = LeafRevealController(enabled=startup_reveal and self._motion)
        self._leaf_retirement = LeafRetirementController(enabled=startup_reveal and self._motion)

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
        reasons = stage_reasons or {}
        self._add_stage_reasons(tree, reasons)
        lines = render_tree(tree, unmanaged=unmanaged, cwd_segments=cwd_segments)
        self._observe_statuses(projection)
        self._observe_marks(marks or {})
        self._sync_retirement(projection)
        self._participant_detail = "description" if participant_detail == "description" else "cwd"
        self._cwd_segments = cwd_segments
        self._lines_data = lines
        self._revision += 1
        self._stale = projection.stale
        self._reconcile(lines)

        keys = self.selectable_keys
        preferred = ("p", selected_id) if selected_id is not None else None
        if self._selected_key not in keys:
            self._selected_key = (
                preferred
                if preferred in keys
                else keys[min(old_index, len(keys) - 1)]
                if keys
                else None
            )
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
            self._selected_key = key
            self._apply_selection()
        return self.selected_participant_id

    def select_key(self, key: Key) -> str | None:
        if key in self.selectable_keys:
            self._selected_key = key
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
        self._selected_key = keys[max(0, min(len(keys) - 1, index + offset))]
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
        if isinstance(widget, Label):
            widget.update(label)
            return widget
        if key[0] == "s":
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
            if not isinstance(widget, AgentLeaf):
                continue
            participant_id = node.get("id")
            managed = _is_managed_key(key)
            staged = (managed and participant_id == self._staged_id) or (
                not managed and key[1] == self._staged_unmanaged_id
            )
            trajectory = managed and participant_id == self._trajectory_id and not staged
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
                    node.get("status") in wanted or (isinstance(mark, RowMark) and mark.attention)
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

    def leaf_keys(self) -> tuple[Key, ...]:
        return tuple(
            key
            for key, widget in self._key_widgets.items()
            if isinstance(widget, AgentLeaf | EmptyTreeState)
        )

    def reveal_widths(self, keys: Collection[Key]) -> dict[Key, int]:
        selected = set(keys)
        return {
            key: widget.required_reveal_width
            for key, widget in self._key_widgets.items()
            if key in selected and isinstance(widget, AgentLeaf | EmptyTreeState)
        }

    def set_reveals(self, reveals: dict[Key, int]) -> None:
        self._reveals = dict(reveals)
        for key, widget in self._key_widgets.items():
            if isinstance(widget, AgentLeaf | EmptyTreeState):
                widget.set_reveal(self._reveals.get(key))

    def _sync_reveal(self) -> None:
        keys = self.leaf_keys()
        if not self._leaf_reveal.needs_sync(keys):
            return
        requested = self._leaf_reveal.sync_keys(keys)
        frame = self._leaf_reveal.observe(
            self.reveal_widths(requested),
            animate_new=self._animate_new,
        )
        self.set_reveals(frame.widths)
        if frame.active and self._leaf_reveal_timer is None:
            self._leaf_reveal_timer = self.set_interval(
                REGIE_STARTUP_REVEAL_INTERVAL_SECONDS, self._tick_reveal
            )

    def _tick_reveal(self) -> None:
        keys = self.leaf_keys()
        frame = self._leaf_reveal.tick(self.reveal_widths(self._leaf_reveal.sync_keys(keys)))
        self.set_reveals(frame.widths)
        if not frame.active:
            self._stop_reveal()

    def _stop_reveal(self) -> None:
        if self._leaf_reveal_timer is not None:
            self._leaf_reveal_timer.stop()
            self._leaf_reveal_timer = None

    def _sync_retirement(self, projection: StateProjection) -> None:
        current = {
            ("p", participant.participant_id): bool(participant.parent_id)
            for participant in projection.participants.values()
        }
        # A row gone from the projection stays until its sweep has ended, even if it is already
        # dead: the sweep is what tells the human what happened.
        departed = {key for key in self._key_widgets if key[0] == "p" and key not in current}
        for key in departed & self._kills:
            if not self._wipes.has(key):
                # A kill too quick for its mark to be seen.
                self._wipes.start(key, "$error", seconds=REGIE_WIPE_DEATH_SECONDS)
        self._kills -= departed
        held = {key for key in departed if self._wipes.has(key)}
        self._wipes.cancel(departed - held)
        self._ensure_wipe_timer()
        returned = {key for key in self._held if key in current}
        change = self._leaf_retirement.observe(current)
        self._leaf_reveal.cancel(change.retire)
        if not self._leaf_reveal.active:
            self._stop_reveal()
        self.restore_retiring(change.restore | returned)
        if (change.restore or returned) and not self._leaf_retirement.active:
            self._stop_retirement()
        if held:
            for key, width in self.retire(held).items():
                self._held[key] = (key in change.retire, width)
        if change.retire - held:
            frame = self._leaf_retirement.begin(
                self.retire(change.retire - held), candidates=change.retire - held
            )
            self._apply_retirement(frame)

    def _release_held(self, key: Key) -> None:
        """Its sweep is over: the row now retires, with the reverse reveal if it may, or goes."""
        if key not in self._held:  # a row retiring on its own is not ours to cut short
            return
        animate, width = self._held.pop(key)
        widget = self._retiring.get(key)
        if widget is None:
            return
        if animate:
            self._apply_retirement(self._leaf_retirement.begin({key: width}, candidates={key}))
            return
        self._retiring.pop(key)
        self._retiring_predecessors.pop(key, None)
        self._remove_widget(widget)
        if not self._lines_data and not self._retiring:
            self._reconcile_empty()

    def retire(self, keys: Collection[Key]) -> dict[Key, int]:
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
        for key in keys:
            self._held.pop(key, None)
            widget = self._retiring.pop(key, None)
            if widget is not None:
                self._retiring_predecessors.pop(key, None)
                widget.set_reveal(None)
                self._key_widgets[key] = widget

    def announce_kill(self, participant_id: str) -> None:
        """A kill was asked for: the row will leave with the reverse reveal, roots included, and
        a red sweep first if its termination mark is never seen."""
        key = ("p", participant_id)
        self._leaf_retirement.allow(key)
        self._kills.add(key)

    def _apply_retirement(self, frame: LeafRetirementFrame) -> None:
        for key, reveal in frame.widths.items():
            widget = self._retiring.get(key)
            if widget is not None:
                widget.set_reveal(reveal)
        for key in frame.completed:
            widget = self._retiring.pop(key, None)
            if widget is not None:
                self._retiring_predecessors.pop(key, None)
                self._remove_widget(widget)
        if frame.active and self._leaf_retirement_timer is None:
            self._leaf_retirement_timer = self.set_interval(
                REGIE_STARTUP_REVEAL_INTERVAL_SECONDS, self._tick_retirement
            )
        elif not frame.active:
            self._stop_retirement()
            if not self._lines_data:
                self._reconcile_empty()

    def _tick_retirement(self) -> None:
        self._apply_retirement(self._leaf_retirement.tick())

    def _stop_retirement(self) -> None:
        if self._leaf_retirement_timer is not None:
            self._leaf_retirement_timer.stop()
            self._leaf_retirement_timer = None

    def _observe_statuses(self, projection: StateProjection) -> None:
        """Sweep a row whose status changed; a row seen for the first time is the reveal's."""
        current = {
            ("p", participant.participant_id): participant.status
            for participant in projection.participants.values()
        }
        for key, status in current.items():
            previous = self._statuses.get(key)
            if previous is None or previous == status:
                continue
            if (color := status_wipe_color(previous, status)) is not None:
                self._wipes.start(
                    key, color, seconds=REGIE_WIPE_DEATH_SECONDS if status == "dead" else None
                )
        now = time.monotonic()
        self._attention_since = {
            key: self._attention_since.get(key, now)
            for key, status in current.items()
            if status == "awaiting_input"
        }
        self._statuses = current
        self._ensure_wipe_timer()

    def _observe_marks(self, marks: Mapping[str, RowMark]) -> None:
        """Sweep a row whose operation just produced something worth announcing.

        A spawn ends green; a kill is red; a delivery green; a refusal yellow. (A spawn begins
        cyan, but that is the spawn row opening, not a mark.)
        """
        previous = self._mark_kinds
        # What the first projection already carries happened before the human looked.
        if previous is not None:
            for participant_id, mark in marks.items():
                if mark.kind is MarkKind.TERMINATING:
                    self._leaf_retirement.allow(("p", participant_id))
                if previous.get(participant_id) is not mark.kind and (
                    wipe := _MARK_WIPES.get(mark.kind)
                ):
                    self._wipes.start(("p", participant_id), wipe[0], seconds=wipe[1])
            for participant_id, kind in previous.items():
                # Up: a rolled-back spawn has no row left to sweep.
                if (
                    kind is MarkKind.LAUNCHING
                    and participant_id not in marks
                    and ("p", participant_id) in self._statuses
                ):
                    self._wipes.start(("p", participant_id), "$success")
        self._mark_kinds = {participant_id: mark.kind for participant_id, mark in marks.items()}
        self._ensure_wipe_timer()

    def _sync_mark_seen(self) -> None:
        """Once the cursor has rested on a failure for a moment, report it seen."""
        key = self._selected_key
        mark = next(
            (
                node.get("mark")
                for _, node, line_key, _, _ in self._lines_data
                if line_key == key and _is_managed_key(line_key)
            ),
            None,
        )
        wanted = mark.operation_id if isinstance(mark, RowMark) and mark.needs_seen else None
        if wanted == self._seen_pending:
            return
        self._stop_mark_seen()
        if wanted is not None:
            self._seen_pending = wanted
            self._seen_timer = self.set_timer(
                REGIE_MARK_SEEN_SECONDS, lambda: self._report_seen(wanted)
            )

    def _report_seen(self, operation_id: str) -> None:
        self._seen_timer = None
        self._seen_pending = None
        self.post_message(self.MarkSeen(operation_id))

    def _stop_mark_seen(self) -> None:
        if self._seen_timer is not None:
            self._seen_timer.stop()
            self._seen_timer = None
        self._seen_pending = None

    def _ensure_wipe_timer(self) -> None:
        if self._wipes.active and self._wipe_timer is None:
            self._wipe_timer = self.set_interval(REGIE_WIPE_INTERVAL_SECONDS, self._tick_wipe)

    def flash(self, participant_id: str, color: str) -> None:
        """Sweep one row in *color* to show something just landed on it."""
        key = ("p", participant_id)
        if key in self._key_widgets:
            self._wipes.start(key, color)
            self._ensure_wipe_timer()

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

    def _tick_wipe(self) -> None:
        frame = self._wipes.tick()
        for key, wipe in frame.running.items():
            if isinstance(widget := self._sweepable(key), AgentLeaf | SpawnLeaf):
                widget.set_wipe(wipe)
        for key in frame.finished:
            if isinstance(widget := self._sweepable(key), AgentLeaf | SpawnLeaf):
                widget.set_wipe(None)
            self._release_held(key)
        if not frame.active and self._wipe_timer is not None:
            self._wipe_timer.stop()
            self._wipe_timer = None

    def _sweepable(self, key: Key) -> Widget | None:
        """The row a sweep draws on: a held row is no longer in the tree but is still drawn."""
        return self._key_widgets.get(key) or self._retiring.get(key)

    def _sync_attention(self) -> None:
        """Breathe the newest few waiting rows; the rest hold the steady tint."""
        if not self._attention_since or not self._motion:
            self._stop_attention()
            return
        if self._attention_timer is None:
            self._attention_timer = self.set_interval(
                REGIE_ATTENTION_PULSE_INTERVAL, self._tick_attention
            )

    def _tick_attention(self) -> None:
        self._attention_phase = (self._attention_phase + 1) % len(self._ATTENTION_BREATH)
        newest = sorted(self._attention_since, key=self._attention_since.__getitem__, reverse=True)
        breathing = set(newest[:REGIE_ATTENTION_PULSE_LIMIT])
        level = self._ATTENTION_BREATH[self._attention_phase]
        for key in self._attention_since:
            if isinstance(widget := self._key_widgets.get(key), AgentLeaf):
                widget.set_attention_level(level if key in breathing else 0)

    def _stop_attention(self) -> None:
        if self._attention_timer is not None:
            self._attention_timer.stop()
            self._attention_timer = None

    def _remove_widget(self, widget: Widget) -> None:
        widget.remove()

    def on_unmount(self) -> None:
        self._stop_reveal()
        self._stop_retirement()
        self._stop_attention()
        self._stop_mark_seen()
        self._held.clear()
        if self._wipe_timer is not None:
            self._wipe_timer.stop()
            self._wipe_timer = None
        self._leaf_retirement.clear()


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
