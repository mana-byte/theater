"""The card under the tree appears only in the rows the tree leaves free."""

from __future__ import annotations

import pytest
from regie.motions.routes import AwaitMarks
from regie.widgets import ParticipantTree

from packages.regie.tests.support.app_fakes import build_app
from packages.regie.tests.support.waiting import wait_until


def _text(tree: ParticipantTree) -> str:
    card = tree._card
    return "\n".join(card.render_line(y).text.rstrip() for y in range(card.size.height))


@pytest.mark.asyncio
async def test_the_card_describes_the_row_in_focus_and_follows_the_cursor() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree.select("participant-1")
        await wait_until(pilot, lambda: tree._card.display and "first" in _text(tree))

        tree.select("participant-2")
        await wait_until(pilot, lambda: "second" in _text(tree))


@pytest.mark.asyncio
async def test_the_card_says_who_the_agent_waits_on_and_who_waits_on_it() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree.select("participant-1")

        tree.set_await_marks(
            AwaitMarks(
                waiting={"participant-1": "$accent"},
                awaited={"participant-2": 1},
                waiting_on={"participant-1": ("participant-2",)},
                waited_by={"participant-2": ("participant-1",)},
            )
        )
        await wait_until(pilot, lambda: "waiting on second" in _text(tree))
        tree.select("participant-2")
        await wait_until(pilot, lambda: "waited on by first" in _text(tree))


@pytest.mark.asyncio
async def test_the_card_gives_way_when_the_tree_needs_the_rows() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        await wait_until(pilot, lambda: tree._card.display)

        await pilot.resize_terminal(52, 12)  # two agents and the spawn row fill what is left
        await wait_until(pilot, lambda: not tree._card.display)

        await pilot.resize_terminal(52, 30)
        await wait_until(pilot, lambda: tree._card.display)


@pytest.mark.asyncio
async def test_the_ambience_plays_only_in_the_rows_the_card_leaves() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        await wait_until(pilot, lambda: tree._card.display)
        app._free_band_cache = None
        rows = 2 * 3 + 1  # two agents and the spawn row

        assert app._free_band()[1] == tree.size.height - rows - tree.card_rows
