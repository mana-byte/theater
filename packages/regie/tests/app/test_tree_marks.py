"""Rows wearing what the daemon reports about operations aimed at them."""

from __future__ import annotations

import pytest
from regie.widgets import ParticipantTree
from regie.widgets.leaf import AgentLeaf

from packages.regie.tests.support.app_fakes import (
    build_app,
    make_operation,
    make_projection,
    with_operations,
)
from packages.regie.tests.support.waiting import wait_until


def _leaf(tree: ParticipantTree, participant_id: str) -> AgentLeaf:
    leaf = tree._key_widgets[("p", participant_id)]
    assert isinstance(leaf, AgentLeaf)
    return leaf


def _rows(leaf: AgentLeaf) -> list[str]:
    return [leaf.render_line(row).text.strip() for row in range(3)]


async def _publish(app, pilot, *operations) -> None:
    app._state.projection = with_operations(app._state.projection, *operations)
    app._show_projection(app._state.projection)
    await pilot.pause()


async def test_a_spawn_in_flight_fills_its_ring_and_names_the_phase() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-2")

        await _publish(
            app,
            pilot,
            make_operation("spawn", "running", "workspace_ready", target="participant-2"),
        )
        await wait_until(pilot, lambda: "◑" in _rows(leaf)[1])
        assert _rows(leaf)[2].endswith("workspace ready")

        await _publish(
            app,
            pilot,
            make_operation("spawn", "succeeded", "terminal_bound", target="participant-2"),
        )
        await wait_until(pilot, lambda: "◑" not in _rows(leaf)[1])
        assert "workspace ready" not in _rows(leaf)[2]


async def test_a_refused_send_marks_its_target_and_says_why() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-2")

        await _publish(
            app,
            pilot,
            make_operation(
                "controls.send",
                "failed",
                "delivery_rejected",
                target="participant-2",
                error_code="human_present",
            ),
        )
        await wait_until(pilot, lambda: "⊘" in _rows(leaf)[1])
        assert _rows(leaf)[2].endswith("blocked · human present")
        await wait_until(pilot, lambda: leaf._wipe is not None)  # the row announces it
        assert leaf._wipe[1] == "$warning"


async def test_a_failure_stops_asking_once_the_cursor_has_rested_on_it(monkeypatch) -> None:
    monkeypatch.setattr("regie.widgets.tree.REGIE_MARK_SEEN_SECONDS", 0.05)
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-2")
        await _publish(
            app,
            pilot,
            make_operation(
                "controls.send",
                "failed",
                "delivery_rejected",
                target="participant-2",
                error_code="internal",
            ),
        )
        await wait_until(pilot, lambda: "✗" in _rows(leaf)[1])

        tree.select("participant-1")
        await pilot.pause(0.2)
        assert "✗" in _rows(leaf)[1]  # not looked at yet: it stays

        tree.select("participant-2")
        await wait_until(pilot, lambda: "✗" not in _rows(leaf)[1])
        assert "failed" not in _rows(leaf)[2]


async def test_a_delivered_send_shows_a_tick_that_fades_on_its_own(monkeypatch) -> None:
    monkeypatch.setattr("regie.marks.REGIE_MARK_SENT_SECONDS", 0.3)
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-2")

        await _publish(
            app,
            pilot,
            make_operation(
                "controls.send", "succeeded", "delivery_acknowledged", target="participant-2"
            ),
        )
        await wait_until(pilot, lambda: "✓" in _rows(leaf)[1])
        await wait_until(
            pilot, lambda: "✓" not in _rows(leaf)[1]
        )  # nothing re-rendered it but the timer


@pytest.mark.parametrize(
    ("kind", "state", "phase", "error", "glyph"),
    [
        ("controls.send", "failed", "delivery_rejected", "internal", "✗"),
        ("controls.send", "failed", "delivery_rejected", "busy", "!"),
        ("controls.send", "uncertain", "delivery_unknown", None, "!"),
    ],
)
async def test_a_folded_heading_shows_a_hidden_agents_failed_operation(
    tmp_path, kind, state, phase, error, glyph
) -> None:
    from regie.paths import RegiePaths
    from regie.tree_layout import TreeLayout

    separator_id = "sep:1234abcd"
    path = RegiePaths(tmp_path).tree_layout_path
    TreeLayout(
        orders={"": [separator_id, "participant-1", "participant-2"]},
        separators={separator_id: {"name": "Backend", "collapsed": True}},
    ).save(path)
    projection = with_operations(
        make_projection(),
        make_operation(kind, state, phase, target="participant-2", error_code=error),
    )
    app, _client, _presentation = build_app(tree_layout_path=path, projection=projection)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: ("s", separator_id) in tree._key_widgets)
        heading = tree._key_widgets[("s", separator_id)]
        await wait_until(pilot, lambda: glyph in heading.render_line(1).text)
