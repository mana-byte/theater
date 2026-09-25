"""Tree selection and cursor navigation between the tree and footer."""

from __future__ import annotations

from regie.app_parts._shared import _AppBase
from regie.render.layout import Key
from regie.ui_constants import (
    REGIE_USAGE_METRIC_DOWN,
    REGIE_USAGE_METRIC_LEFT,
    REGIE_USAGE_METRIC_RIGHT,
    REGIE_USAGE_METRIC_UP,
)
from regie.widgets import ParticipantTree


class TreeNavigation(_AppBase):
    def _selected_id(self) -> str | None:
        if self._usage_panel.in_footer:
            return None
        projection = self._state.projection
        selected = self.query_one(ParticipantTree).selected_participant_id
        return selected if projection is not None and selected in projection.participants else None

    def _selected_unmanaged_pane(self) -> str | None:
        if self._usage_panel.in_footer:
            return None
        return self.query_one(ParticipantTree).selected_unmanaged_pane

    def select_participant(self, participant_id: str) -> None:
        """Select a stable participant ID from a pointer interaction."""
        projection = self._state.projection
        if projection is None or participant_id not in projection.participants:
            return
        if self._usage_panel.in_footer:
            self._leave_usage_metrics()
        self._navigation.select(participant_id)
        self.query_one(ParticipantTree).select(participant_id)

    def select_tree_item(self, key: Key, item_id: str) -> None:
        """Select one rendered tree row without treating a local pane as a participant."""
        if self._usage_panel.in_footer:
            self._leave_usage_metrics()
        tree = self.query_one(ParticipantTree)
        tree.select_key(key)
        if key[0] == "p" and tree.selected_participant_id == item_id:
            self._navigation.select(item_id)

    def _move_selection(self, offset: int) -> None:
        tree = self.query_one(ParticipantTree)
        tree.move(offset)
        if tree.selected_participant_id is not None:
            self._navigation.select(tree.selected_participant_id)

    def action_cursor_down(self) -> None:
        metric = self._usage_panel.keyboard_metric
        if metric is not None:
            target = REGIE_USAGE_METRIC_DOWN.get(metric)
            if target is not None:
                self._select_usage_metric(target, origin=metric)
            return
        tree = self.query_one(ParticipantTree)
        keys = tree.selectable_keys
        if keys and tree.selected_key != keys[-1]:
            self._move_selection(1)
        else:
            self._select_usage_metric("input")

    def action_cursor_up(self) -> None:
        metric = self._usage_panel.keyboard_metric
        if metric is not None:
            if metric in REGIE_USAGE_METRIC_UP:
                target = self._usage_panel.keyboard_origin or REGIE_USAGE_METRIC_UP[metric]
                self._select_usage_metric(target)
            else:
                self._leave_usage_metrics()
            return
        self._move_selection(-1)

    def action_cursor_left(self) -> None:
        metric = self._usage_panel.keyboard_metric
        target = REGIE_USAGE_METRIC_LEFT.get(metric or "")
        if target is not None:
            self._select_usage_metric(target)

    def action_cursor_right(self) -> None:
        metric = self._usage_panel.keyboard_metric
        target = REGIE_USAGE_METRIC_RIGHT.get(metric or "")
        if target is not None:
            self._select_usage_metric(target)

    async def action_cursor_left_or_trajectory(self) -> None:
        if self._usage_panel.in_footer:
            self.action_cursor_left()
            return
        if self._trajectory_has_focus():
            return
        if (work := self._trajectory_request("left")) is not None:
            await self._presentation_queue.run("trajectory", work)

    async def action_cursor_right_or_focus(self) -> None:
        if self._usage_panel.in_footer:
            self.action_cursor_right()
            return
        if self._trajectory_has_focus():
            return
        await self.action_focus_stage()

    def _restore_tree_focus(self) -> None:
        if not self.is_running:
            return
        self.set_focus(None)
        self.query_one(ParticipantTree).set_cursor_visible(True)
