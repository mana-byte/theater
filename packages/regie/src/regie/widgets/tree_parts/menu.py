"""The options menu closing the participant tree: what is open, and where the cursor settles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from regie.menu import ROOT_ID, MenuView
from regie.render.layout import Key, RenderedLine, TreeLines
from regie.widgets.menu_row import MenuRow
from regie.widgets.tree_parts._host import _TreeBase


def _is_menu_key(key: Key | None) -> bool:
    """Whether a row belongs to the options menu closing the tree."""
    return key is not None and key[0] in {"g", "m"}


def _is_selectable_key(key: Key) -> bool:
    return key[0] in {"p", "s", "u", "g", "m"}


def selectable_keys(lines: Sequence[RenderedLine]) -> tuple[Key, ...]:
    return tuple(key for _, _, key, _, _ in lines if _is_selectable_key(key))


def settle(
    keys: Sequence[Key], current: Key | None, preferred: Key | None, old_index: int
) -> Key | None:
    """Keep the cursor on a row that exists; never fall into the menu when an agent remains."""
    if current in keys:
        return current
    if preferred in keys:
        return preferred
    if _is_menu_key(current) and any(_is_menu_key(key) for key in keys):
        return ("g", ROOT_ID)  # an option went away: back to the menu's root
    rows = [key for key in keys if not _is_menu_key(key)] or list(keys)
    return rows[min(old_index, len(rows) - 1)] if rows else None


class TreeMenu(_TreeBase):
    """Open, change and close the menu; the widget draws it."""

    def _in_menu(self) -> bool:
        return _is_menu_key(self._selected_key)

    @property
    def in_menu(self) -> bool:
        """Whether the cursor is on the options menu (the root, an option or a choice)."""
        return self._in_menu()

    @property
    def menu_return(self) -> Key | None:
        """The row the cursor was on before it entered the menu."""
        return self._menu_return

    @property
    def menu_view(self) -> MenuView:
        return self._menu_view

    def _set_selected(self, key: Key | None) -> None:
        """Move the cursor, opening the menu as it enters and closing it as it leaves."""
        entered, left = (
            not self._in_menu() and _is_menu_key(key),
            self._in_menu() and not _is_menu_key(key),
        )
        if entered:
            self._menu_return = self._selected_key
        if left:
            self._menu_view, self._menu_return = MenuView(), None
        self._selected_key = key
        if entered or left:
            self._rerender()

    def _settle_lines(
        self, lines: TreeLines, opened: bool, preferred: Key | None = None, old_index: int = 0
    ) -> TreeLines:
        """Settle the cursor on *lines*, drawn with the menu *opened*; redraw them once if the
        settled cursor entered or left the menu. The cursor moves; what the menu had open stays."""
        self._selected_key = settle(
            selectable_keys(lines), self._selected_key, preferred, old_index
        )
        return lines if self._in_menu() == opened else self._render_lines(not opened)

    def open_menu(self, option: str | None = None) -> None:
        """Put the cursor in the menu: on its root, or on the first choice of *option*, unfolded."""
        if self._menu_hidden:  # a new agent is asking for its directory
            return
        if option is None:
            self._set_selected(("g", ROOT_ID))
            self._apply_selection()
            return
        self._menu_view = replace(self._menu_view, expanded=self._menu_view.expanded | {option})
        if not self._in_menu():
            self._menu_return = self._selected_key
        lines = self._render_lines(True)  # the menu's rows read what it was entered from
        first = next(
            (key for key in selectable_keys(lines) if key[1].startswith(f"{option}/")), None
        )
        self._selected_key = first or ("m", option)
        self._show_lines(lines)  # the explicit choice stands, even on an option now gone
        self._apply_selection()

    def close_menu(self) -> Key | None:
        """Take the cursor back to the row it left, or the nearest agent if that row is gone."""
        back = self._menu_return
        rows = [key for key in self.selectable_keys if not _is_menu_key(key)]
        if back not in rows:
            back = rows[-1] if rows else None
        self._set_selected(back)
        self._apply_selection()
        return back

    def menu_root(self) -> MenuRow | None:
        """The ``+ options`` row, where the filter is typed."""
        root = self._key_widgets.get(("g", ROOT_ID))
        return root if isinstance(root, MenuRow) else None

    def set_menu_view(self, view: MenuView) -> None:
        """Change what is opened in the menu (an option unfolded, a list shown whole, a filter)."""
        if view != self._menu_view:
            self._menu_view = view
            if self._in_menu():
                self._rerender()

    def refresh_menu(self) -> None:
        """Redraw the menu after what it holds changed (a list finished loading, a theme picked)."""
        if self._in_menu():
            self._rerender()
