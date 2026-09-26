"""A tree row with one field that can be edited in place."""

from __future__ import annotations

from rich.cells import cell_len
from textual import events
from textual.widgets import Static

from regie.widgets.name_editor import InlineEditor, NameEditor


class RenameableRow(Static):
    """Host one inline editor over a field; subclasses locate, seed, and commit it."""

    #: Which rendered row of this widget the field sits on.
    _edit_row = 1
    #: Whether Enter on the untouched opening value still commits (a prefilled directory does).
    _commit_unchanged = False
    _name_editor: InlineEditor | None = None
    _rename_original = ""

    def _name_span(self) -> tuple[int, int] | None:
        raise NotImplementedError

    def _rename_value(self) -> str:
        raise NotImplementedError

    def _commit_rename(self, name: str) -> None:
        raise NotImplementedError

    def _render_label(self):
        raise NotImplementedError

    def _make_editor(self, value: str) -> InlineEditor:
        return NameEditor(value, submit=self._rename_submitted, cancel=self._rename_cancelled)

    def _edit_closed(self, *, committed: bool) -> None:
        """Called once the editor leaves, whether by Enter, Esc, a click away, or a refresh."""

    def _renaming_changed(self) -> None:
        """Redraw so the field under the editor is blank while it is open."""
        self.update(self._render_label(), layout=False)

    @property
    def renaming(self) -> bool:
        return self._name_editor is not None

    def _name_clicked(self, event: events.Click) -> bool:
        span = self._name_span()
        offset = event.get_content_offset(self)
        return (
            span is not None
            and offset is not None
            and offset.y == self._edit_row
            and span[0] <= offset.x < span[1]
        )

    async def begin_rename(self) -> None:
        """Open the inline editor over this row's field."""
        if self._name_editor is not None or self._name_span() is None:
            return
        self._rename_original = self._rename_value()
        editor = self._make_editor(self._rename_original)
        self._name_editor = editor
        self._renaming_changed()
        await self.mount(editor)
        self._sync_rename_geometry()
        editor.focus()
        if not self._commit_unchanged:
            editor.select_all()

    def _sync_rename_geometry(self) -> None:
        """Keep a live editor over its field as the row's layout changes."""
        editor = self._name_editor
        span = self._name_span()
        if editor is None or span is None:
            return
        editor.styles.offset = (span[0], self._edit_row)
        editor.styles.width = cell_len(editor.value) + 1
        # A row not laid out yet has no width; cap the editor only once there is one.
        width = self.content_size.width
        editor.styles.max_width = max(1, width - span[0]) if width else None

    def _on_resize(self, _event: events.Resize) -> None:
        self._sync_rename_geometry()

    def _rename_submitted(self, value: str) -> None:
        """Commit a non-empty value, unless it is the untouched opening value."""
        self._name_editor = None
        self._renaming_changed()
        name = value.strip()
        committed = bool(name) and (self._commit_unchanged or name != self._rename_original)
        if committed:
            self._commit_rename(name)
        self._edit_closed(committed=committed)

    def _rename_cancelled(self) -> None:
        self._name_editor = None
        self._renaming_changed()
        self._edit_closed(committed=False)

    def cancel_rename(self) -> None:
        """Leave edit mode as Esc would."""
        if self._name_editor is not None:
            self._name_editor.action_cancel()

    def close_rename(self) -> None:
        """Detach a live editor quietly, e.g. when this row leaves the projection."""
        editor = self._name_editor
        if editor is None:
            return
        self._name_editor = None
        editor.close()
        self._renaming_changed()
        self._edit_closed(committed=False)


__all__ = ["RenameableRow"]
