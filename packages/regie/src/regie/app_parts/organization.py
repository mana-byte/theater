"""Régie-local participant tree organization actions."""

from __future__ import annotations

from regie.app_parts._shared import _AppBase, logger
from regie.tree import participant_groups
from regie.tree_layout import TreeLayout
from regie.widgets import ParticipantTree
from regie.widgets.prompts import ControlPromptScreen
from theater.frontend import StateProjection


class TreeOrganization(_AppBase):
    def _initialize_tree_layout(self) -> None:
        if self._tree_layout_path is None:
            return
        self._tree_layout, warning = TreeLayout.load(self._tree_layout_path)
        if warning is not None:
            logger.warning(warning)
            self.notify(warning, title="tree layout", severity="warning", timeout=10)

    def action_move_tree_row(self, offset: int) -> None:
        if self._usage_panel.in_footer:
            return
        tree = self.query_one(ParticipantTree)
        key = tree.selected_key
        projection = self._state.projection
        if key is None or key[0] not in {"p", "s"} or projection is None:
            return
        context = self._organization_context(key, projection)
        if context is None:
            return
        parent_id, siblings = context
        if not self._tree_layout.move(
            parent_id,
            key[1],
            offset,
            siblings,
            projection.participants,
        ):
            return
        self._save_tree_layout()
        self._show_projection(projection)

    def action_add_separator(self) -> None:
        if self._usage_panel.in_footer:
            return
        tree = self.query_one(ParticipantTree)
        key = tree.selected_key
        projection = self._state.projection
        if key is None or key[0] not in {"p", "s"} or projection is None:
            return
        context = self._organization_context(key, projection)
        if context is None:
            return

        def receive(name: str | None) -> None:
            if name is None:
                return
            current = self._state.projection
            if current is None:
                return
            current_context = self._organization_context(key, current)
            if current_context is None:
                return
            parent_id, siblings = current_context
            separator_id = self._tree_layout.insert_separator(
                parent_id,
                key[1],
                name,
                siblings,
                current.participants,
            )
            self._save_tree_layout()
            self._show_projection(current)
            self.query_one(ParticipantTree).select_key(("s", separator_id))

        self.push_screen(ControlPromptScreen("Add separator", "separator name"), receive)

    def rename_separator(self, separator_id: str, name: str) -> None:
        if not self._tree_layout.rename_separator(separator_id, name):
            return
        self._save_tree_layout()
        if (projection := self._state.projection) is not None:
            self._show_projection(projection)

    def delete_separator(self, separator_id: str) -> None:
        if not self._tree_layout.delete_separator(separator_id):
            return
        self._save_tree_layout()
        if (projection := self._state.projection) is not None:
            self._show_projection(projection)

    def _organization_context(
        self,
        key: tuple[str, str],
        projection: StateProjection,
    ) -> tuple[str | None, list[str]] | None:
        if key[0] == "p":
            participant = projection.participants.get(key[1])
            if participant is None:
                return None
            parent_id = (
                participant.parent_id if participant.parent_id in projection.participants else None
            )
        elif key[0] == "s" and key[1] in self._tree_layout.separators:
            parent_key = self._tree_layout.parent_key_for_separator(key[1])
            if parent_key is None:
                return None
            parent_id = parent_key or None
            if parent_id is not None and parent_id not in projection.participants:
                return None
        else:
            return None
        siblings = participant_groups(projection).siblings.get(parent_id, [])
        if key[1] not in self._tree_layout.ordered(parent_id, siblings):
            return None
        return parent_id, siblings

    def _save_tree_layout(self) -> None:
        if self._tree_layout_path is None:
            return
        try:
            self._tree_layout.save(self._tree_layout_path)
        except OSError as exc:
            logger.warning("tree layout save failed: %s", exc)
            self.notify(f"tree layout save failed: {exc}", severity="warning")


__all__ = ["TreeOrganization"]
