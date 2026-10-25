"""The tree's last row: an invitation to spawn that works like ``o``."""

from __future__ import annotations

from textual import events
from textual.content import Content

from regie.widgets.chrome import NonSelectableStatic


class SpawnRow(NonSelectableStatic):
    """Click it to pick a harness, the same as pressing the key shown beside it."""

    can_focus = False

    DEFAULT_CSS = """
    SpawnRow { height: 1; padding: 0 2; }
    SpawnRow:hover { text-style: bold; }
    """

    def set_label(self, label: Content) -> None:
        self.update(label, layout=False)

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        await self.run_action("app.spawn")


__all__ = ["SpawnRow"]
