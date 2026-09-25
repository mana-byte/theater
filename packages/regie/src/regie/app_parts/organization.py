"""Régie-local participant tree organization actions."""

from __future__ import annotations

from regie.app_parts._shared import _AppBase, logger
from regie.paths import paths_from_environment
from regie.tree_layout import TreeLayout
from regie.widgets import ParticipantTree


class TreeOrganization(_AppBase):
    def _initialize_tree_layout(self) -> None:
        path = paths_from_environment().tree_layout_path
        self._tree_layout, warning = TreeLayout.load(path)
        if warning is not None:
            logger.warning(warning)
            self.notify(warning, title="tree layout", severity="warning", timeout=10)

    def action_move_tree_row(self, offset: int) -> None:
        tree = self.query_one(ParticipantTree)
        key = tree.selected_key
        projection = self._state.projection
        if key is None or key[0] != "p" or projection is None:
            return
        participant = projection.participants.get(key[1])
        if participant is None:
            return
        parent_id = (
            participant.parent_id if participant.parent_id in projection.participants else None
        )
        insertion = {
            participant_id: index for index, participant_id in enumerate(projection.participants)
        }
        sibling_rows = [
            item
            for item in projection.participants.values()
            if (item.parent_id if item.parent_id in projection.participants else None) == parent_id
        ]
        sibling_rows.sort(
            key=lambda item: (
                item.created_at is None,
                item.created_at or 0.0,
                insertion[item.participant_id],
            )
        )
        siblings = [item.participant_id for item in sibling_rows]
        if not self._tree_layout.move(parent_id, key[1], offset, siblings):
            return
        self._save_tree_layout()
        self._show_projection(projection)

    def _save_tree_layout(self) -> None:
        try:
            self._tree_layout.save(paths_from_environment().tree_layout_path)
        except OSError as exc:
            logger.warning("tree layout save failed: %s", exc)
            self.notify(f"tree layout save failed: {exc}", severity="warning")


__all__ = ["TreeOrganization"]
