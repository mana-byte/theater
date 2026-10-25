"""Rows wearing what the daemon reports about operations aimed at them."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest
from regie.ui_constants import REGIE_WIPE_DEATH_SECONDS
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


async def test_opening_the_spawn_row_sweeps_it_cyan() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2 and bool(app._harnesses))

        app.spawn_harness("codex")
        await wait_until(pilot, lambda: ("n", "new") in tree._key_widgets)
        row = tree._key_widgets[("n", "new")]
        await wait_until(pilot, lambda: row._wipe is not None)
        assert row._wipe[1] == "$secondary"
        await wait_until(pilot, lambda: row._wipe is None)  # and the sweep ends


async def test_a_launching_row_sweeps_green_once_it_is_up_and_not_when_it_begins() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-2")

        await _publish(
            app,
            pilot,
            make_operation("spawn", "running", "launch_preparing", target="participant-2"),
        )
        await wait_until(pilot, lambda: "◔" in _rows(leaf)[1])
        assert leaf._wipe is None  # the menu already greeted this spawn

        await _publish(
            app,
            pilot,
            make_operation("spawn", "succeeded", "terminal_bound", target="participant-2"),
        )
        await wait_until(pilot, lambda: leaf._wipe is not None)
        assert leaf._wipe[1] == "$success"


async def test_a_kill_in_flight_sweeps_red_and_shows_that_it_is_terminating() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-2")

        await _publish(
            app,
            pilot,
            make_operation(
                "participants.terminate", "running", "termination_preparing", target="participant-2"
            ),
        )
        await wait_until(pilot, lambda: "✕" in _rows(leaf)[1])
        assert _rows(leaf)[2].endswith("terminating")
        await wait_until(pilot, lambda: leaf._wipe is not None)
        assert leaf._wipe[1] == "$error"
        assert tree._wipes._sweeps[("p", "participant-2")].seconds == REGIE_WIPE_DEATH_SECONDS


def _without(app, participant_id: str) -> None:
    """The daemon forgets *participant_id*, as it does once an exit is verified."""
    remaining = {
        key: item
        for key, item in app._state.projection.participants.items()
        if key != participant_id
    }
    app._state.projection = replace(app._state.projection, participants=MappingProxyType(remaining))
    app._show_projection(app._state.projection)


async def test_a_killed_row_stays_until_its_sweep_ends_then_retires() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree._leaf_retirement._enabled = True
        key = ("p", "participant-2")
        leaf = _leaf(tree, "participant-2")
        await _publish(
            app,
            pilot,
            make_operation("participants.terminate", "running", "accepted", target="participant-2"),
        )
        await wait_until(pilot, lambda: leaf._wipe is not None)  # the red sweep is running

        # The daemon already says it is gone; the sweep has not finished.
        _without(app, "participant-2")
        assert key in tree._retiring and key in tree._held
        assert not tree._leaf_retirement.active  # not shrinking yet
        assert leaf._wipe is not None
        assert "participant-2" not in tree.participant_ids

        await wait_until(pilot, lambda: key not in tree._held)  # the sweep ended...
        await wait_until(pilot, lambda: key not in tree._retiring)  # ...then it retired
        await wait_until(pilot, lambda: leaf not in list(tree.query(AgentLeaf)))


async def test_a_kill_too_quick_for_its_mark_still_sweeps_red_before_the_row_goes() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree._leaf_retirement._enabled = True
        key = ("p", "participant-2")
        leaf = _leaf(tree, "participant-2")

        tree.announce_kill("participant-2")  # what Régie does when you press the key
        _without(app, "participant-2")  # no terminating mark was ever shown
        assert key in tree._held
        await wait_until(pilot, lambda: leaf._wipe is not None)
        assert leaf._wipe[1] == "$error"
        assert tree._wipes._sweeps[key].seconds == REGIE_WIPE_DEATH_SECONDS
        await wait_until(pilot, lambda: key not in tree._retiring)


async def test_a_dead_row_that_is_not_a_kill_lingers_for_its_sweep_then_just_goes() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree._leaf_retirement._enabled = True
        key = ("p", "participant-1")  # a root: not eligible for the reverse reveal
        participants = dict(app._state.projection.participants)
        participants["participant-1"] = replace(participants["participant-1"], status="dead")
        app._state.projection = replace(
            app._state.projection, participants=MappingProxyType(participants)
        )
        app._show_projection(app._state.projection)
        leaf = _leaf(tree, "participant-1")
        await wait_until(pilot, lambda: leaf._wipe is not None)

        _without(app, "participant-1")
        assert key in tree._retiring
        await wait_until(pilot, lambda: key not in tree._retiring)
        assert not tree._leaf_retirement.active  # never animated: it was not a kill


async def test_a_held_row_whose_participant_returns_is_restored_not_duplicated() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree._leaf_retirement._enabled = True
        key = ("p", "participant-2")
        original = app._state.projection
        leaf = _leaf(tree, "participant-2")
        tree.announce_kill("participant-2")
        _without(app, "participant-2")
        assert key in tree._held

        app._state.projection = original  # the exit was never verified after all
        app._show_projection(original)
        assert key not in tree._held and key not in tree._retiring
        assert tree._key_widgets[key] is leaf
        assert len([w for w in tree.query(AgentLeaf) if w.key == key]) == 1


async def test_without_motion_a_killed_row_goes_at_once() -> None:
    app, _client, _presentation = build_app(reduce_motion=True)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree.announce_kill("participant-2")
        _without(app, "participant-2")
        assert ("p", "participant-2") not in tree._held
        await wait_until(pilot, lambda: ("p", "participant-2") not in tree._key_widgets)
