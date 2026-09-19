"""Reconciled, animated participant tree backed by public state."""

from __future__ import annotations

import contextlib
from collections.abc import Collection, Mapping
from typing import Literal

from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label

from regie.animations.retirement import LeafRetirementController, LeafRetirementFrame
from regie.animations.reveal import LeafRevealController
from regie.animations.routes import LeafOverlay
from regie.render.layout import Key, is_root_prefix, render_tree
from regie.tree import tree_for_projection
from regie.ui_constants import REGIE_EMPTY_TREE_KEY, REGIE_STARTUP_REVEAL_INTERVAL_SECONDS
from regie.widgets.chrome import EmptyTreeState
from regie.widgets.leaf import AgentLeaf
from regie.widgets.usage_breakdown import UsageBreakdownPanel
from theater.frontend import StateProjection


def _is_participant_key(key: Key) -> bool:
    return key[0] == "p"


class ParticipantTree(VerticalScroll):
    """Keep one interactive leaf per stable public participant ID."""

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

    def __init__(self, *args, startup_reveal: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lines_data: list[tuple[Content, dict, Key, str, str]] = []
        self._key_widgets: dict[Key, Widget] = {}
        self._overlaid: set[Key] = set()
        self._reveals: dict[Key, int] = {}
        self._retiring: dict[Key, AgentLeaf] = {}
        self._retiring_predecessors: dict[Key, Key | None] = {}
        self._selected_id: str | None = None
        self._staged_id: str | None = None
        self._trajectory_id: str | None = None
        self._participant_detail: Literal["cwd", "description"] = "cwd"
        self._cwd_segments = 2
        self._stale = False
        self._cursor_visible = True
        self._revision = 0
        self._animate_new: set[Key] = set()
        self._leaf_reveal = LeafRevealController(enabled=startup_reveal)
        self._leaf_retirement = LeafRetirementController(enabled=startup_reveal)
        self._leaf_reveal_timer: Timer | None = None
        self._leaf_retirement_timer: Timer | None = None

    @property
    def selected_id(self) -> str | None:
        return self._selected_id

    @property
    def participant_ids(self) -> tuple[str, ...]:
        return tuple(key[1] for _, _, key, _, _ in self._lines_data if _is_participant_key(key))

    @property
    def tree_lines(self) -> list[tuple[Content, dict, Key, str, str]]:
        return self._lines_data

    @property
    def revision(self) -> int:
        return self._revision

    def configure(self, *, startup_reveal: bool) -> None:
        if self._leaf_reveal.started:
            return
        self._leaf_reveal = LeafRevealController(enabled=startup_reveal)
        self._leaf_retirement = LeafRetirementController(enabled=startup_reveal)

    def show_projection(
        self,
        projection: StateProjection,
        *,
        participant_detail: str = "cwd",
        cwd_segments: int = 2,
        stage_reasons: Mapping[str, str] | None = None,
        selected_id: str | None = None,
        staged_id: str | None = None,
        trajectory_id: str | None = None,
    ) -> str | None:
        tree = tree_for_projection(projection)
        reasons = stage_reasons or {}
        self._add_stage_reasons(tree, reasons)
        lines = render_tree(tree, cwd_segments=cwd_segments)
        self._sync_retirement(projection)
        self._participant_detail = "description" if participant_detail == "description" else "cwd"
        self._cwd_segments = cwd_segments
        self._lines_data = lines
        self._revision += 1
        self._stale = projection.stale
        self._reconcile(lines)

        ids = self.participant_ids
        preferred = selected_id if selected_id in ids else self._selected_id
        self._selected_id = preferred if preferred in ids else (ids[0] if ids else None)
        self._staged_id = staged_id
        self._trajectory_id = trajectory_id
        self._apply_selection()
        self._animate_new = {
            ("p", participant.participant_id)
            for participant in projection.participants.values()
            if participant.parent_id in projection.participants
        }
        self._sync_reveal()
        return self._selected_id

    @staticmethod
    def _add_stage_reasons(nodes: list[dict[str, object]], reasons: Mapping[str, str]) -> None:
        pending = list(nodes)
        while pending:
            node = pending.pop()
            participant_id = node.get("id")
            if isinstance(participant_id, str) and participant_id in reasons:
                node["stage_reason"] = reasons[participant_id]
            children = node.get("children")
            if isinstance(children, list):
                pending.extend(item for item in children if isinstance(item, dict))

    def select(self, participant_id: str | None) -> str | None:
        if participant_id in self.participant_ids:
            self._selected_id = participant_id
            self._apply_selection()
        return self._selected_id

    def move(self, offset: int) -> str | None:
        ids = self.participant_ids
        if not ids:
            self._selected_id = None
            return None
        try:
            index = ids.index(self._selected_id) if self._selected_id is not None else 0
        except ValueError:
            index = 0
        self._selected_id = ids[max(0, min(len(ids) - 1, index + offset))]
        self._apply_selection()
        return self._selected_id

    def mark_surfaces(self, *, staged_id: str | None, trajectory_id: str | None) -> None:
        self._staged_id = staged_id
        self._trajectory_id = trajectory_id
        self._apply_selection()

    def set_cursor_visible(self, visible: bool) -> None:
        self._cursor_visible = visible
        self._apply_selection()

    def _reconcile(self, lines: list[tuple[Content, dict, Key, str, str]]) -> None:
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
        node: dict,
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
        if isinstance(widget, Label):
            widget.update(label)
            return widget
        if _is_participant_key(key):
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
            if not isinstance(widget, AgentLeaf):
                continue
            participant_id = node.get("id")
            staged = participant_id == self._staged_id
            trajectory = participant_id == self._trajectory_id and not staged
            widget.set_class(staged, "tree-staged")
            widget.set_class(trajectory, "tree-trajectory-staged")
            widget.set_stage_marker("tmux" if staged else "trajectory" if trajectory else None)
            widget.set_cursor(self._cursor_visible and participant_id == self._selected_id)
        self.scroll_to_selection()

    def scroll_to_selection(self) -> None:
        key = ("p", self._selected_id or "")
        widget = self._key_widgets.get(key)
        if widget is not None:
            with contextlib.suppress(Exception):
                self.scroll_to_widget(widget)

    def set_overlays(self, overlays: dict[Key, LeafOverlay]) -> None:
        for key in self._overlaid - set(overlays):
            widget = self._key_widgets.get(key)
            if isinstance(widget, AgentLeaf):
                widget.set_overlay(None)
        for key, cells in overlays.items():
            widget = self._key_widgets.get(key)
            if isinstance(widget, AgentLeaf):
                widget.set_overlay(cells)
        self._overlaid = set(overlays)

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
        participants = {
            ("p", participant.participant_id): participant.parent_id in projection.participants
            for participant in projection.participants.values()
        }
        change = self._leaf_retirement.observe(participants)
        self._leaf_reveal.cancel(change.retire)
        if not self._leaf_reveal.active:
            self._stop_reveal()
        self.restore_retiring(change.restore)
        if change.restore and not self._leaf_retirement.active:
            self._stop_retirement()
        if change.retire:
            frame = self._leaf_retirement.begin(
                self.retire(change.retire), candidates=change.retire
            )
            self._apply_retirement(frame)

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
            widget = self._retiring.pop(key, None)
            if widget is not None:
                self._retiring_predecessors.pop(key, None)
                widget.set_reveal(None)
                self._key_widgets[key] = widget

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

    def _remove_widget(self, widget: Widget) -> None:
        widget.remove()

    def on_unmount(self) -> None:
        self._stop_reveal()
        self._stop_retirement()
        self._leaf_retirement.clear()


class TreeStack(Vertical):
    """Tree surface with a height-clamped usage overlay."""

    def on_resize(self, event: events.Resize) -> None:
        with contextlib.suppress(Exception):
            panel = self.query_one(UsageBreakdownPanel)
            if panel.has_class("-visible"):
                panel.constrain_to_height(event.size.height)


__all__ = ["ParticipantTree", "TreeStack", "_is_participant_key"]
