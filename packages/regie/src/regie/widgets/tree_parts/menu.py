"""The options menu closing the participant tree: what is open, and where the cursor settles."""

from __future__ import annotations

from dataclasses import replace

from regie.menu import ROOT_ID, MenuView
from regie.render.layout import Key
from regie.widgets.menu_row import MenuRow
from regie.widgets.tree_parts._host import _TreeBase


def _is_menu_key(key: Key | None) -> bool:
    """Whether a row belongs to the options menu closing the tree."""
    return key is not None and key[0] in {"g", "m"}


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

    def _settle_selection(self, selected_id: str | None, old_index: int) -> None:
        """Keep the cursor on a row that exists; never fall into the menu when an agent remains."""
        keys = self.selectable_keys
        if self._selected_key in keys:
            return
        preferred = ("p", selected_id) if selected_id is not None else None
        rows = [key for key in keys if not _is_menu_key(key)] or list(keys)
        if preferred in keys:
            self._selected_key = preferred
        elif _is_menu_key(self._selected_key) and any(_is_menu_key(key) for key in keys):
            self._selected_key = ("g", ROOT_ID)  # an option went away: back to the menu's root
        else:
            self._selected_key = rows[min(old_index, len(rows) - 1)] if rows else None

    def open_menu(self, option: str | None = None) -> None:
        """Put the cursor in the menu: on its root, or on the first choice of *option*, unfolded."""
        if self._menu_hidden:  # a new agent is asking for its directory
            return
        if option is not None:
            self._menu_view = replace(self._menu_view, expanded=self._menu_view.expanded | {option})
        self._set_selected(("g", ROOT_ID))
        if option is not None:
            self._rerender()
            first = next(
                (key for key in self.selectable_keys if key[1].startswith(f"{option}/")), None
            )
            self._set_selected(first or ("m", option))
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
