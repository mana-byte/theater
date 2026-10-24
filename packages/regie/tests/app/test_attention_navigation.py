"""`]` and `[` jump the cursor between rows that want the human."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from regie.paths import RegiePaths
from regie.tree_layout import TreeLayout
from regie.widgets import ParticipantTree

from packages.regie.tests.support.app_fakes import (
    build_app,
    make_operation,
    make_participant,
    make_projection,
    with_operations,
)
from packages.regie.tests.support.waiting import wait_until


def _projection(*statuses: str):
    people = [
        make_participant(f"participant-{n}", name=f"agent{n}", status=status)
        for n, status in enumerate(statuses, 1)
    ]
    return replace(
        make_projection(),
        participants=MappingProxyType({item.participant_id: item for item in people}),
    )


async def test_brackets_walk_the_rows_that_want_the_human_and_wrap() -> None:
    projection = _projection("idle", "awaiting_input", "working", "dead", "idle")
    app, _client, _presentation = build_app(projection=projection)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        tree.select("participant-1")

        visited = []
        for _ in range(3):
            await pilot.press("]")
            visited.append(tree.selected_participant_id)
        assert visited == ["participant-2", "participant-4", "participant-2"]  # wraps round

        await pilot.press("[")
        assert tree.selected_participant_id == "participant-4"  # and walks back


async def test_a_failed_send_counts_as_wanting_the_human() -> None:
    projection = with_operations(
        _projection("idle", "idle", "idle"),
        make_operation(
            "controls.send",
            "failed",
            "delivery_rejected",
            target="participant-3",
            error_code="busy",
        ),
    )
    app, _client, _presentation = build_app(projection=projection)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 3)
        tree.select("participant-1")
        await pilot.press("]")
        assert tree.selected_participant_id == "participant-3"


async def test_nothing_wanting_the_human_leaves_the_cursor_alone() -> None:
    app, _client, _presentation = build_app(projection=_projection("idle", "working"))

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree.select("participant-2")
        await pilot.press("]", "[")
        assert tree.selected_participant_id == "participant-2"


async def test_a_fold_hiding_a_waiting_agent_is_where_the_cursor_lands(tmp_path: Path) -> None:
    separator_id = "sep:1234abcd"
    path = RegiePaths(tmp_path).tree_layout_path
    TreeLayout(
        orders={"": ["participant-1", separator_id, "participant-2"]},
        separators={separator_id: {"name": "Backend", "collapsed": True}},
    ).save(path)
    app, _client, _presentation = build_app(
        tree_layout_path=path, projection=_projection("idle", "awaiting_input")
    )

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        key = ("s", separator_id)
        await wait_until(pilot, lambda: key in tree.selectable_keys)
        tree.select("participant-1")
        await pilot.press("]")
        assert tree.selected_key == key
