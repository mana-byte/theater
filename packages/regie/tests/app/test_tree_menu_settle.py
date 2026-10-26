"""Where the cursor settles when the rows under it change while it is in the options menu."""

from __future__ import annotations

import pytest
from regie.menu import Loaded, MenuItem, MenuView
from regie.widgets import ParticipantTree
from regie.widgets.tree_parts.menu import settle

from packages.regie.tests.support.app_fakes import build_app
from packages.regie.tests.support.waiting import wait_until

ROOT = ("g", "options")
FIRST = ("p", "participant-1")
A, B = ("p", "a"), ("p", "b")
PENDING = {"harness": "codex", "icon": None, "status": "idle", "name": "new agent"}


def _items() -> tuple[MenuItem, ...]:
    harnesses = Loaded((MenuItem("codex", "codex"), MenuItem("vibe", "vibe")))
    return (
        MenuItem("spawn", "Spawn", choices=harnesses),
        MenuItem("bus", "Show bus panel", run=lambda: None),
    )


async def _in_spawn(app, pilot) -> ParticipantTree:
    """The cursor on Spawn's first choice, entered from the first agent."""
    tree = app.query_one(ParticipantTree)
    tree.menu_source = _items
    await wait_until(pilot, lambda: ROOT in tree._key_widgets)
    tree.select("participant-1")
    tree.open_menu("spawn")
    await wait_until(pilot, lambda: tree.selected_key == ("m", "spawn/codex"))
    return tree


async def test_a_preferred_agent_outranks_a_vanished_menu_choice_and_keeps_the_menu_state() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = await _in_spawn(app, pilot)
        view = tree.menu_view

        tree.menu_source = lambda: _items()[1:]  # the Spawn option is gone
        tree.show_projection(app._state.projection, selected_id="participant-2")

        assert tree.selected_key == ("p", "participant-2") and not tree.in_menu
        assert ("m", "bus") not in tree.selectable_keys  # the menu closed behind the cursor
        assert tree.menu_view == view and tree.menu_return == FIRST  # implicit: kept


async def test_a_deliberate_exit_clears_what_the_menu_had_open() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = await _in_spawn(app, pilot)

        tree.select_key(("p", "participant-2"))

        assert not tree.in_menu and ("m", "bus") not in tree.selectable_keys
        assert tree.menu_view == MenuView() and tree.menu_return is None


async def test_opening_an_option_lists_it_with_the_row_the_cursor_came_from_already_known() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)
        tree.select("participant-1")
        returns: list[object] = []

        def recording() -> tuple[MenuItem, ...]:
            returns.append(tree.menu_return)
            return _items()

        tree.menu_source = recording
        tree.open_menu("spawn")

        assert returns and set(returns) == {FIRST}
        assert tree.selected_key == ("m", "spawn/codex") and tree.menu_return == FIRST


async def test_opening_the_menu_does_nothing_while_a_new_agent_hides_it() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)
        tree.show_projection(
            app._state.projection, selected_id="participant-1", pending_spawn=PENDING
        )
        calls: list[None] = []
        tree.menu_source = lambda: calls.append(None) or _items()
        before = (tree.revision, tree.selected_key, tree.menu_view, tree.menu_return)

        tree.open_menu("spawn")

        assert (tree.revision, tree.selected_key, tree.menu_view, tree.menu_return) == before
        assert calls == [] and ROOT not in tree.selectable_keys


async def test_a_new_agent_arriving_while_in_the_menu_moves_the_cursor_and_keeps_the_menu() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = await _in_spawn(app, pilot)
        view = tree.menu_view

        tree.show_projection(app._state.projection, pending_spawn=PENDING)

        assert tree.selected_key == ("p", "participant-2")  # the old index, clamped to the agents
        assert ("n", "new") in tree._key_widgets and ROOT not in tree.selectable_keys
        assert tree.menu_view == view and tree.menu_return == FIRST


def _count_reconciles(tree: ParticipantTree) -> list[None]:
    calls: list[None] = []
    reconcile = tree._reconcile

    def counting(lines) -> None:
        calls.append(None)
        reconcile(lines)

    tree._reconcile = counting  # type: ignore[method-assign]
    return calls


async def test_each_redraw_reconciles_the_rows_once_even_when_the_cursor_leaves_the_menu() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        tree.menu_source = _items
        await wait_until(pilot, lambda: ROOT in tree._key_widgets)
        tree.select("participant-1")
        reconciles, revision = _count_reconciles(tree), tree.revision

        tree.open_menu("spawn")
        assert (len(reconciles), tree.revision) == (1, revision + 1)
        assert tree.selected_key == ("m", "spawn/codex")

        tree.menu_source = lambda: _items()[1:]
        tree.show_projection(app._state.projection, selected_id="participant-2")
        assert (len(reconciles), tree.revision) == (2, revision + 2)
        assert tree.selected_key == ("p", "participant-2")
        assert ("m", "bus") not in tree._key_widgets  # the one reconcile drew the menu closed

        tree.show_projection(app._state.projection, pending_spawn=PENDING)
        assert (len(reconciles), tree.revision) == (3, revision + 3)
        tree.open_menu("spawn")  # hidden while the new agent asks for its directory
        assert (len(reconciles), tree.revision) == (3, revision + 3)


@pytest.mark.parametrize(
    ("current", "preferred", "old_index", "settled"),
    [
        (A, B, 0, A),  # the cursor's row is still there
        (("m", "gone"), B, 0, B),  # a preferred agent outranks a vanished menu choice
        (("m", "gone"), None, 0, ROOT),  # else the menu keeps the cursor, on its root
        (("p", "gone"), None, 1, B),  # an agent gone: the row at its old index
        (("p", "gone"), None, 9, B),  # clamped to the last agent, never into the menu
    ],
)
def test_settle_keeps_the_cursor_on_a_row_that_exists(current, preferred, old_index, settled):
    assert settle((A, B, ROOT), current, preferred, old_index) == settled


def test_settle_falls_into_the_menu_only_when_no_agent_remains():
    assert settle((ROOT,), ("p", "gone"), None, 3) == ROOT
    assert settle((), ("p", "gone"), None, 0) is None
