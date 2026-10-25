"""The options menu's keys and clicks: they act only while the cursor is in it."""

from __future__ import annotations

import pytest
from regie.widgets import ParticipantTree
from regie.widgets.name_editor import DirectoryEditor

from packages.regie.tests.support.app_fakes import build_app
from packages.regie.tests.support.waiting import wait_until

ROOT = ("g", "options")
CODEX = ("m", "spawn/codex")


async def _ready(app, pilot) -> ParticipantTree:
    tree = app.query_one(ParticipantTree)
    await wait_until(pilot, lambda: app._catalog_ready.is_set() and ROOT in tree._key_widgets)
    return tree


@pytest.mark.asyncio
async def test_o_opens_the_menu_on_the_first_harness_and_x_goes_back_where_the_cursor_was() -> None:
    app, client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")

        await pilot.press("o")
        await wait_until(pilot, lambda: tree.selected_key == CODEX)
        assert tree.menu_return == ("p", "participant-1")

        await pilot.press("x")  # inside the menu x closes it; it does not kill anything
        await wait_until(pilot, lambda: tree.selected_key == ("p", "participant-1"))
        assert not tree.in_menu and client.participants.terminated == []


@pytest.mark.asyncio
async def test_outside_the_menu_the_same_keys_still_act_on_the_agent() -> None:
    app, client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-2")

        await pilot.press("x")
        await wait_until(pilot, lambda: client.participants.terminated == ["participant-2"])


@pytest.mark.asyncio
async def test_enter_on_a_harness_leaves_the_menu_and_asks_for_the_directory() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")
        await pilot.press("o")
        await wait_until(pilot, lambda: tree.selected_key == CODEX)

        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(app.query(DirectoryEditor)))
        assert not tree.in_menu and ROOT not in tree._key_widgets  # the menu steps aside


@pytest.mark.asyncio
async def test_a_sub_menu_unfolds_on_enter_l_or_a_click_and_h_folds_it_back() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "spawn") in tree._key_widgets)
        await pilot.press("j")
        assert tree.selected_key == ("m", "spawn")

        await pilot.press("l")  # unfolds and walks in
        await wait_until(pilot, lambda: tree.selected_key == CODEX)

        await pilot.press("h")  # a choice folds its option and goes up to it
        await wait_until(pilot, lambda: tree.selected_key == ("m", "spawn"))
        assert CODEX not in tree._key_widgets

        await pilot.press("space")  # toggles without moving
        await wait_until(pilot, lambda: CODEX in tree._key_widgets)
        assert tree.selected_key == ("m", "spawn")

        await pilot.press("h")  # an unfolded option folds
        await wait_until(pilot, lambda: CODEX not in tree._key_widgets)
        await pilot.press("h")  # a folded one goes up to the menu
        await wait_until(pilot, lambda: tree.selected_key == ROOT)

        await pilot.click(tree._key_widgets[("m", "spawn")])  # a click unfolds in place
        await wait_until(pilot, lambda: CODEX in tree._key_widgets)
        assert tree.selected_key == ("m", "spawn")


@pytest.mark.asyncio
async def test_a_toggle_option_does_its_thing_and_closes_the_menu() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "bus") in tree._key_widgets)
        tree.select_key(("m", "bus"))
        assert not app.bus_visible

        await pilot.press("enter")
        await wait_until(pilot, lambda: app.bus_visible)
        assert tree.selected_key == ("p", "participant-1") and not tree.in_menu


@pytest.mark.asyncio
async def test_add_separator_works_on_the_row_the_cursor_came_from() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-2")
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "separator") in tree._key_widgets)
        tree.select_key(("m", "separator"))

        await pilot.press("enter")
        await wait_until(pilot, lambda: any(key[0] == "s" for key in tree.selectable_keys))
        assert not tree.in_menu


@pytest.mark.asyncio
async def test_add_separator_is_disabled_when_the_menu_was_entered_from_nowhere() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        item = next(item for item in app._menu_items() if item.id == "separator")
        assert not item.enabled  # no origin row yet: the cursor has not been to the menu
