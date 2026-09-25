"""A tree row whose row-2 name can be edited in place."""

from __future__ import annotations

from textual import events
from textual.widgets import Static

from regie.widgets.name_editor import NameEditor


class RenameableRow(Static):
    """Host one NameEditor over the row's name; subclasses locate and commit the name."""

    _name_editor: NameEditor | None = None
    _rename_original = ""

    def _name_span(self) -> tuple[int, int] | None:
        raise NotImplementedError

    def _rename_value(self) -> str:
        raise NotImplementedError

    def _commit_rename(self, name: str) -> None:
        raise NotImplementedError

    @property
    def renaming(self) -> bool:
        return self._name_editor is not None

    def _name_clicked(self, event: events.Click) -> bool:
        span = self._name_span()
        offset = event.get_content_offset(self)
        return (
            span is not None
            and offset is not None
            and offset.y == 1
            and span[0] <= offset.x < span[1]
        )

    async def begin_rename(self) -> None:
        """Open the inline editor over this row's name."""
        if self._name_editor is not None or self._name_span() is None:
            return
        self._rename_original = self._rename_value()
        editor = NameEditor(
            self._rename_original, submit=self._rename_submitted, cancel=self._rename_cancelled
        )
        self._name_editor = editor
        await self.mount(editor)
        self._sync_rename_geometry()
        editor.focus()
        editor.select_all()

    def _sync_rename_geometry(self) -> None:
        """Keep a live editor over the name as the row's layout changes."""
        editor = self._name_editor
        span = self._name_span()
        if editor is None or span is None:
            return
        editor.styles.offset = (span[0], 1)
        editor.styles.width = max(12, self.content_size.width - span[0])

    def _rename_submitted(self, value: str) -> None:
        """Commit a non-empty name that differs from the one the editor opened with."""
        self._name_editor = None
        name = value.strip()
        if name and name != self._rename_original:
            self._commit_rename(name)

    def _rename_cancelled(self) -> None:
        self._name_editor = None

    def cancel_rename(self) -> None:
        """Leave rename mode as Esc would."""
        if self._name_editor is not None:
            self._name_editor.action_cancel()

    def close_rename(self) -> None:
        """Detach a live editor quietly, e.g. when this row leaves the projection."""
        editor = self._name_editor
        if editor is None:
            return
        self._name_editor = None
        editor.close()


__all__ = ["RenameableRow"]
