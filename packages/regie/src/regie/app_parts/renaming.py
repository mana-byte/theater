"""Inline rename of an agent's live-only alias or a separator's name in the tree."""

from __future__ import annotations

from textual import events

from regie.app_parts._shared import _AppBase
from regie.widgets import ParticipantTree
from regie.widgets.name_editor import NameEditor
from regie.widgets.renameable import RenameableRow


class RenameActions(_AppBase):
    async def action_rename(self) -> None:
        """Open the inline name editor on the selected agent or separator."""
        if self._usage_panel.in_footer:
            return
        tree = self.query_one(ParticipantTree)
        key = tree.selected_key
        if key is None:
            self.notify("no participant selected", severity="warning")
            return
        row = tree._key_widgets.get(key)
        if key[0] not in {"p", "s"} or not isinstance(row, RenameableRow):
            self.notify("only agents and separators can be renamed", severity="warning")
            return
        await row.begin_rename()

    def submit_rename(self, participant_id: str, name: str) -> None:
        """Submit one inline rename through the same lane as other actions."""
        self._start_action(self._actions.rename(participant_id, name))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Clicks on non-focusable chrome never blur the editor, so leave rename mode here.
        self._cancel_rename_outside(event.screen_x, event.screen_y)

    def _cancel_rename_outside(self, x: int, y: int) -> None:
        for editor in self.screen.query(NameEditor):
            if not editor.region.contains(x, y):
                editor.action_cancel()


__all__ = ["RenameActions"]
