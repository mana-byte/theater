"""Inline rename of a participant's live-only alias in the tree."""

from __future__ import annotations

from regie.app_parts._shared import _AppBase
from regie.widgets import ParticipantTree
from regie.widgets.leaf import AgentLeaf


class RenameActions(_AppBase):
    async def action_rename(self) -> None:
        """Open the inline name editor on the selected agent, or rename the selected separator."""
        if self._usage_panel.in_footer:
            return
        tree = self.query_one(ParticipantTree)
        key = tree.selected_key
        if key is None:
            self.notify("no participant selected", severity="warning")
            return
        if key[0] == "s":
            self.rename_separator(key[1])
            return
        if key[0] != "p":
            self.notify("only managed participants can be renamed", severity="warning")
            return
        for leaf in tree.query(AgentLeaf):
            if leaf.key == key:
                await leaf.begin_rename()
                return

    def submit_rename(self, participant_id: str, name: str) -> None:
        """Submit one inline rename through the same lane as other actions."""
        self._start_action(self._actions.rename(participant_id, name))


__all__ = ["RenameActions"]
