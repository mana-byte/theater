"""The options menu closing the tree: a line while folded, open while the cursor is in it."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest
from regie.menu import Loaded, MenuItem
from regie.widgets import ParticipantTree

from packages.regie.tests.support.app_fakes import build_app, make_projection
from packages.regie.tests.support.waiting import wait_until

ROOT = ("g", "options")


def _items() -> tuple[MenuItem, ...]:
    harnesses = Loaded((MenuItem("codex", "codex"), MenuItem("vibe", "vibe")))
    return (
        MenuItem("spawn", "Spawn", choices=harnesses),
        MenuItem("bus", "Show bus panel", run=lambda: None),
    )


def _height(tree: ParticipantTree, key) -> int:
    return tree._key_widgets[key].styles.height.value  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_the_menu_is_one_line_until_the_cursor_reaches_it_and_three_while_it_is_there() -> (
    None
):
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        tree.menu_source = _items
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)
        assert _height(tree, ROOT) == 1 and not tree.in_menu

        tree.select("participant-2")  # the last agent
        await pilot.press("j")
        await wait_until(pilot, lambda: tree.in_menu and _height(tree, ROOT) == 3)
        assert tree.menu_return == ("p", "participant-2")
        assert ("m", "spawn") in tree._key_widgets and ("m", "bus") in tree._key_widgets

        await pilot.press("j", "j")  # the options are rows the cursor walks like any other
        assert tree.selected_key == ("m", "bus")

        await press_up(pilot, tree)
        await wait_until(pilot, lambda: not tree.in_menu and _height(tree, ROOT) == 1)
        assert ("m", "spawn") not in tree._key_widgets and tree.menu_return is None


async def press_up(pilot, tree) -> None:
    while tree.in_menu:
        await pilot.press("k")


@pytest.mark.asyncio
async def test_options_stay_folded_and_a_sub_menu_unfolds_only_when_asked() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        tree.menu_source = _items
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)

        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "spawn") in tree._key_widgets)
        assert ("m", "spawn/codex") not in tree._key_widgets

        tree.open_menu("spawn")  # what `o` will do: Spawn unfolded, the cursor on its first choice
        await wait_until(pilot, lambda: ("m", "spawn/codex") in tree._key_widgets)
        assert tree.selected_key == ("m", "spawn/codex")


@pytest.mark.asyncio
async def test_closing_the_menu_puts_the_cursor_back_where_it_was_and_folds_everything() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        tree.menu_source = _items
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)
        tree.select("participant-1")

        tree.open_menu("spawn")
        await wait_until(pilot, lambda: ("m", "spawn/codex") in tree._key_widgets)

        assert tree.close_menu() == ("p", "participant-1")
        await wait_until(pilot, lambda: ("m", "spawn/codex") not in tree._key_widgets)
        assert tree.selected_key == ("p", "participant-1") and not tree.menu_view.expanded


@pytest.mark.asyncio
async def test_a_menu_pick_that_vanishes_leaves_the_cursor_on_the_menu_not_an_agent() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        tree.menu_source = _items
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)
        tree.open_menu("spawn")
        await wait_until(pilot, lambda: tree.selected_key == ("m", "spawn/codex"))

        tree.menu_source = lambda: _items()[1:]  # the Spawn option is gone
        tree.refresh_menu()
        await wait_until(pilot, lambda: ("m", "spawn") not in tree._key_widgets)
        assert tree.selected_key == ROOT


@pytest.mark.asyncio
async def test_the_cursor_never_falls_into_the_menu_when_its_agent_goes_away() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree.select("participant-2")

        projection = app._state.projection
        alone = {"participant-1": projection.participants["participant-1"]}
        app._state.projection = replace(projection, participants=MappingProxyType(alone))
        app._show_projection(app._state.projection)

        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1",))
        assert tree.selected_key == ("p", "participant-1") and not tree.in_menu


@pytest.mark.asyncio
async def test_the_menu_is_reachable_from_an_empty_tree_and_closes_back_to_it() -> None:
    nobody = replace(make_projection(), participants=MappingProxyType({}))
    app, _client, _presentation = build_app(projection=nobody)

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        tree.menu_source = _items
        await pilot.pause()
        assert ROOT not in tree._key_widgets  # the empty state is all there is

        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "spawn") in tree._key_widgets)
        assert tree.close_menu() is None
        await wait_until(pilot, lambda: ROOT not in tree._key_widgets)


@pytest.mark.asyncio
async def test_the_menu_steps_aside_while_a_new_agent_asks_for_its_directory() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)

        app.spawn_harness("codex")
        await wait_until(pilot, lambda: ("n", "new") in tree._key_widgets)
        assert ROOT not in tree._key_widgets

        app.cancel_pending_spawn()
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)


@pytest.mark.asyncio
async def test_clicking_a_menu_row_puts_the_cursor_on_it() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)
        tree.menu_source = _items

        await pilot.click(tree._key_widgets[ROOT])
        await wait_until(pilot, lambda: tree.selected_key == ROOT)
        assert tree.in_menu
