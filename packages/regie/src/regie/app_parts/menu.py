"""The options menu: what it holds, and what its keys and clicks do.

Every pick calls an app method that already existed; this module only decides which rows exist.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from regie.app_parts._shared import _AppBase
from regie.formatting import harness_icon
from regie.menu import Loaded, MenuItem, find_item
from regie.render.layout import Key
from regie.spawn import SpawnChoice
from regie.ui_constants import REGIE_MENU_MORE_ID, REGIE_MENU_PREVIEW_ROWS
from regie.widgets import ParticipantTree


class MenuActions(_AppBase):
    def _initialize_menu(self) -> None:
        self.query_one(ParticipantTree).menu_source = self._menu_items

    def _menu_tree(self) -> ParticipantTree:
        return self.query_one(ParticipantTree)

    def _menu_has_cursor(self) -> bool:
        """Whether keys belong to the menu: its row has the cursor and the footer does not."""
        trees = self.query(ParticipantTree)
        return bool(trees) and trees.first().in_menu and not self._usage_panel.in_footer

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "menu":
            return self._menu_has_cursor()
        return super().check_action(action, parameters)

    # ---- what the menu holds -------------------------------------------------------------

    def _menu_items(self) -> tuple[MenuItem, ...]:
        """The options, built fresh from the app's own state each time the menu is drawn."""
        origin = self._menu_tree().menu_return
        retry = self.latest_uncertain_action()
        items = [
            self._menu_spawn(),
            self._menu_separator(origin),
            MenuItem(
                "bus",
                "Hide bus panel" if self.bus_visible else "Show bus panel",
                "coordination traffic between agents",
                run=self.action_toggle_bus,
            ),
            MenuItem(
                "usage",
                "Hide usage footer" if self.usage_visible else "Show usage footer",
                "tokens and cost",
                run=self.action_toggle_usage,
            ),
        ]
        if retry is not None:
            items.append(
                MenuItem(
                    "retry",
                    f"Retry uncertain {retry.action}",
                    "replay the retained idempotency key",
                    run=self.retry_latest_action,
                )
            )
        items.append(
            MenuItem("quit", "Quit", "restore the terminals and leave", run=self.action_quit)
        )
        return tuple(items)

    def _menu_spawn(self) -> MenuItem:
        if not self._catalog_ready.is_set():
            choices = Loaded(state="loading")
        else:
            choices = Loaded(
                tuple(self._menu_harness(choice) for choice in self._spawn_choices()),
                note="no harness is installed",
            )
        return MenuItem("spawn", "Spawn", "start a fresh session", choices=choices)

    def _menu_harness(self, choice: SpawnChoice) -> MenuItem:
        icon = self.icon_for_harness(choice.harness) or harness_icon(choice.harness)
        return MenuItem(
            choice.harness,
            choice.harness,
            "choose a directory" if choice.enabled else choice.reason or "launch is unavailable",
            glyph=icon,
            enabled=choice.enabled,
            run=partial(self.spawn_harness, choice.harness),
        )

    def _menu_separator(self, origin: Key | None) -> MenuItem:
        fits = origin is not None and origin[0] in {"p", "s"}
        return MenuItem(
            "separator",
            "Add separator",
            "a named divider above the row you came from" if fits else "needs an agent or heading",
            enabled=fits,
            run=self.action_add_separator,
        )

    # ---- keys and clicks ------------------------------------------------------------------

    def action_menu(self, verb: str) -> None:
        """A key pressed while the cursor is in the menu; *verb* is what the binding means."""
        tree = self._menu_tree()
        if verb == "close":
            tree.close_menu()
            return
        key = tree.selected_key
        if key is None:
            return
        {
            "activate": self._menu_activate,
            "expand": self._menu_expand,
            "toggle": self._menu_toggle,
            "fold": self._menu_fold,
        }[verb](tree, key)

    def action_open_menu(self) -> None:
        self._menu_tree().open_menu()

    def on_participant_tree_menu_clicked(self, message: ParticipantTree.MenuClicked) -> None:
        tree = self._menu_tree()
        item = find_item(self._menu_items(), message.key[1])
        if item is not None and item.choices is not None:
            self._menu_toggle(tree, message.key)  # a click unfolds in place; the cursor stays
        else:
            self._menu_activate(tree, message.key)

    def _menu_activate(self, tree: ParticipantTree, key: Key) -> None:
        if key[0] != "m":
            return
        option, _, choice = key[1].partition("/")
        if choice == REGIE_MENU_MORE_ID:
            self._menu_show_all(tree, option)
            return
        item = find_item(self._menu_items(), key[1])
        if item is None:
            return
        if item.choices is not None:
            tree.open_menu(option)  # unfolds it and puts the cursor on its first choice
        elif item.run is not None:
            self._menu_pick(tree, item)

    def _menu_expand(self, tree: ParticipantTree, key: Key) -> None:
        """l / right: unfold only, never run anything."""
        item = find_item(self._menu_items(), key[1]) if key[0] == "m" else None
        if item is not None and item.choices is not None:
            tree.open_menu(item.id)

    def _menu_toggle(self, tree: ParticipantTree, key: Key) -> None:
        item = find_item(self._menu_items(), key[1]) if key[0] == "m" else None
        if item is None or item.choices is None:
            return
        view = tree.menu_view
        folded = view.expanded - {item.id}
        tree.set_menu_view(
            replace(
                view, expanded=folded if item.id in view.expanded else view.expanded | {item.id}
            )
        )

    def _menu_fold(self, tree: ParticipantTree, key: Key) -> None:
        """h / left: a choice folds its option, an unfolded option folds, a folded one goes up."""
        if key[0] != "m":
            return
        option, _, choice = key[1].partition("/")
        view = tree.menu_view
        if choice or option in view.expanded:
            tree.set_menu_view(replace(view, expanded=view.expanded - {option}))
            tree.select_key(("m", option))
        else:
            tree.select_key(("g", "options"))

    def _menu_show_all(self, tree: ParticipantTree, option: str) -> None:
        item = find_item(self._menu_items(), option)
        if item is None or item.choices is None:
            return
        tree.set_menu_view(replace(tree.menu_view, more=tree.menu_view.more | {option}))
        revealed = item.choices.items[REGIE_MENU_PREVIEW_ROWS : REGIE_MENU_PREVIEW_ROWS + 1]
        if revealed:
            tree.select_key(("m", f"{option}/{revealed[0].id}"))

    def _menu_pick(self, tree: ParticipantTree, item: MenuItem) -> None:
        """Leave the menu for the row the cursor came from, then do it; a theme stays open."""
        if item.keeps_menu:
            item.run()  # type: ignore[misc]
            tree.refresh_menu()
            return
        tree.close_menu()
        self.call_later(item.run)  # type: ignore[arg-type]


__all__ = ["MenuActions"]
