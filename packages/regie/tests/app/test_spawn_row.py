"""The tree's last row, an invitation to spawn that never takes the cursor."""

from __future__ import annotations

import pytest
from regie.render.layout import render_tree, row_count
from regie.widgets import ParticipantTree
from regie.widgets.spawn_row import SpawnRow
from textual.command import CommandPalette

from packages.regie.tests.support.app_fakes import build_app
from packages.regie.tests.support.tree_nodes import PARENT, label_rows
from packages.regie.tests.support.waiting import wait_until

KEY = ("g", "spawn")


def _agent(pid: str, **kw) -> dict:
    return {**PARENT, "id": pid, "name": None, "status": "idle", "children": [], **kw}


def test_the_spawn_row_is_one_more_root_so_the_last_agent_stops_being_last():
    lines = render_tree([_agent("aaa"), _agent("bbb")], spawn_row=True)

    keys = [key for _, _, key, _, _ in lines]
    assert keys[-1] == KEY
    assert [prefix for _, _, _, prefix, _ in lines] == ["├── ", "├── ", "└── "]
    assert label_rows(lines[-1][0]) == ["└── + spawn  o"]  # one row, the key that does the same
    assert row_count(KEY) == 1


def test_there_is_no_spawn_row_without_an_agent_or_unless_asked_for():
    assert render_tree([], spawn_row=True) == []
    assert KEY not in [key for _, _, key, _, _ in render_tree([_agent("aaa")])]


@pytest.mark.asyncio
async def test_the_spawn_row_ends_the_tree_and_the_cursor_never_lands_on_it() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: KEY in tree._key_widgets)
        assert isinstance(tree._key_widgets[KEY], SpawnRow)
        assert KEY not in tree.selectable_keys

        tree.move(+10)
        assert tree.selected_key != KEY


@pytest.mark.asyncio
async def test_clicking_the_spawn_row_opens_the_spawn_palette() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: KEY in tree._key_widgets)

        await pilot.click(tree._key_widgets[KEY])
        await wait_until(pilot, lambda: isinstance(app.screen, CommandPalette))


@pytest.mark.asyncio
async def test_the_spawn_row_steps_aside_while_a_new_agent_asks_for_its_directory() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: KEY in tree._key_widgets)

        app.spawn_harness("codex")
        await wait_until(pilot, lambda: ("n", "new") in tree._key_widgets)
        assert KEY not in tree._key_widgets

        app.cancel_pending_spawn()
        await wait_until(pilot, lambda: KEY in tree._key_widgets)
