"""The options menu: what it holds, and what its keys and clicks do.

Every pick calls an app method that already existed; this module only decides which rows exist.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from regie.app_parts._shared import _AppBase
from regie.formatting import harness_icon, short_identifier, tilde
from regie.menu import Loaded, MenuItem, find_item
from regie.render.layout import Key
from regie.resume import ResumeCandidate
from regie.spawn import SpawnChoice
from regie.ui_constants import REGIE_MENU_MORE_ID, REGIE_MENU_PREVIEW_ROWS
from regie.widgets import ParticipantTree
from theater.frontend import TranscriptCandidate


class MenuActions(_AppBase):
    #: The lists an option fetches each time it unfolds; frozen, so a class default is safe.
    _menu_resume: Loaded = Loaded(state="loading")
    _menu_transcripts: Loaded = Loaded(state="loading")
    #: An option unfolded from the keyboard while its list loads: the cursor walks in on arrival.
    _menu_walk_in: str | None = None

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
        recover = self._menu_transcript(origin)
        items = [
            self._menu_spawn(),
            MenuItem(
                "resume",
                "Resume",
                "bring back a dead session",
                choices=self._menu_resume,
            ),
            *([recover] if recover is not None else []),
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
        items += [
            self._menu_theme(),
            MenuItem("quit", "Quit", "restore the terminals and leave", run=self.action_quit),
        ]
        return tuple(items)

    def _menu_dead(self, candidate: ResumeCandidate) -> MenuItem:
        """A session that can come back, drawn as the dead agent it was."""
        who = candidate.name or short_identifier(candidate.participant_id)
        context = " ".join((candidate.description or candidate.spawn_prompt or "").split())
        where = tilde(candidate.cwd) if candidate.cwd else ""
        return MenuItem(
            candidate.participant_id,
            f"{candidate.harness}  {who}",
            candidate.reason or " · ".join(part for part in (where, context) if part),
            glyph="✗",
            style="$error",
            enabled=candidate.available,
            run=partial(self.resume_dead_session, candidate),
        )

    def _menu_transcript(self, origin: Key | None) -> MenuItem | None:
        """Offered only for a row whose transcript identity is not trusted yet."""
        if origin is None or origin[0] != "p" or not self.transcript_recovery_available(origin[1]):
            return None
        return MenuItem(
            "transcript",
            "Recover transcript",
            "bind a transcript the daemon found for that agent",
            choices=self._menu_transcripts,
        )

    def _menu_candidate(
        self, index: int, candidate: TranscriptCandidate, participant_id: str
    ) -> MenuItem:
        state = candidate.rejection_reason or candidate.provenance or "unverified"
        owner = candidate.owner_id or candidate.tombstone_id
        return MenuItem(
            str(index),
            candidate.location,
            f"{state} · owned by {owner}" if owner else state,
            run=partial(self.select_transcript_candidate, candidate, participant_id=participant_id),
        )

    def _menu_theme(self) -> MenuItem:
        """The themes, the current one marked; a pick shows at once and the menu stays open."""
        choices = Loaded(
            tuple(
                MenuItem(
                    name,
                    name,
                    glyph="●" if name == self.theme else " ",
                    style="$secondary",
                    run=partial(self._menu_set_theme, name),
                    keeps_menu=True,
                )
                for name in sorted(self.available_themes)
            )
        )
        return MenuItem("theme", "Theme", "the colour scheme, for this session", choices=choices)

    def _menu_set_theme(self, name: str) -> None:
        self.theme = name

    # ---- lists that load when their option unfolds ----------------------------------------

    def _menu_load(self, option: str) -> None:
        """Start reading what *option* lists, showing ``loading…`` until it arrives."""
        if option == "resume":
            self._menu_resume = Loaded(state="loading")
            self.run_worker(self._menu_fetch_resume(), group="menu-resume", exclusive=True)
        elif option == "transcript":
            origin = self._menu_tree().menu_return
            if origin is None:
                return
            self._transcript_recovery_target = origin[1]
            self._menu_transcripts = Loaded(state="loading")
            self.run_worker(
                self._menu_fetch_transcripts(origin[1]), group="menu-transcript", exclusive=True
            )

    async def _menu_fetch_resume(self) -> None:
        try:
            discovery = await self.load_resume_sessions()
        except Exception:  # a daemon hiccup becomes a row in the list, not a crash
            self._menu_resume = Loaded(state="failed", note="could not read the dead sessions")
        else:
            self._menu_resume = Loaded(
                tuple(self._menu_dead(candidate) for candidate in discovery.candidates),
                note="no dead session to resume",
            )
        self._menu_loaded("resume")

    async def _menu_fetch_transcripts(self, participant_id: str) -> None:
        candidates = await self.load_transcript_candidates()
        self._menu_transcripts = Loaded(
            tuple(self._menu_candidate(i, c, participant_id) for i, c in enumerate(candidates)),
            note="no transcript can be bound",
        )
        self._menu_loaded("transcript")

    def _menu_loaded(self, option: str) -> None:
        """Show a list that just arrived; if the cursor waited on its option, walk it in."""
        tree = self._menu_tree()
        tree.refresh_menu()
        if self._menu_walk_in == option and tree.selected_key == ("m", option):
            first = next((k for k in tree.selectable_keys if k[1].startswith(f"{option}/")), None)
            if first is not None:
                tree.select_key(first)
        if self._menu_walk_in == option:
            self._menu_walk_in = None

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
            self._menu_unfold(tree, option)
        elif item.run is not None:
            self._menu_pick(tree, item)

    def _menu_expand(self, tree: ParticipantTree, key: Key) -> None:
        """l / right: unfold only, never run anything."""
        item = find_item(self._menu_items(), key[1]) if key[0] == "m" else None
        if item is not None and item.choices is not None:
            self._menu_unfold(tree, item.id)

    def _menu_unfold(self, tree: ParticipantTree, option: str) -> None:
        """Unfold *option* with the cursor on its first choice, loading what it lists first."""
        self._menu_load(option)
        self._menu_walk_in = option
        tree.open_menu(option)

    def _menu_toggle(self, tree: ParticipantTree, key: Key) -> None:
        item = find_item(self._menu_items(), key[1]) if key[0] == "m" else None
        if item is None or item.choices is None:
            return
        view = tree.menu_view
        if item.id in view.expanded:
            tree.set_menu_view(replace(view, expanded=view.expanded - {item.id}))
        else:
            self._menu_load(item.id)
            self._menu_walk_in = None  # a click or space unfolds in place
            tree.set_menu_view(replace(view, expanded=view.expanded | {item.id}))

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
