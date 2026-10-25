"""Motion on the tree: sweeps that announce a status change and the tint of rows that need you."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from regie.ui_constants import REGIE_ROUTE_COLORS, REGIE_WAITING_FRAMES
from regie.widgets import ParticipantTree
from regie.widgets.leaf import AgentLeaf

from packages.regie.tests.support.app_fakes import build_app, make_participant, make_projection
from packages.regie.tests.support.waiting import wait_until


def _with_status(projection, participant_id: str, status: str):
    participants = dict(projection.participants)
    participants[participant_id] = replace(participants[participant_id], status=status)
    return replace(projection, participants=MappingProxyType(participants))


async def _change(app, pilot, participant_id: str, status: str) -> None:
    app._state.projection = _with_status(app._state.projection, participant_id, status)
    app._show_projection(app._state.projection)
    await pilot.pause()


def render_text(leaf: AgentLeaf) -> str:
    """The leaf's second row as drawn, rails stripped."""
    return leaf.render_line(1).text.strip().lstrip("├└│─ ")


def _leaf(tree: ParticipantTree, participant_id: str) -> AgentLeaf:
    leaf = tree._key_widgets[("p", participant_id)]
    assert isinstance(leaf, AgentLeaf)
    return leaf


async def test_a_status_change_sweeps_the_row_once_then_clears() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-1")
        assert leaf._wipe is None  # the first sight of a row is the reveal's, not a sweep

        await _change(app, pilot, "participant-1", "working")
        assert leaf._wipe is None  # a turn starting is not announced; finishing it is
        await _change(app, pilot, "participant-1", "idle")
        await wait_until(pilot, lambda: leaf._wipe is not None)
        assert leaf._wipe[1] == "$success"
        assert _leaf(tree, "participant-2")._wipe is None
        await wait_until(pilot, lambda: leaf._wipe is None)
        assert tree._wipe_timer is None  # no timer outlives the sweep


async def test_awaiting_input_holds_a_tint_until_it_is_answered() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-1")
        assert not leaf.has_class("tree-attention")

        await _change(app, pilot, "participant-1", "awaiting_input")
        assert leaf.has_class("tree-attention")
        assert tree._attention_timer is not None  # the newest waiting rows breathe

        await _change(app, pilot, "participant-1", "idle")
        assert not leaf.has_class("tree-attention")
        assert tree._attention_timer is None


async def test_only_the_newest_waiting_rows_breathe() -> None:
    waiting = [
        make_participant(f"participant-{n}", name=f"agent{n}", status="awaiting_input")
        for n in range(1, 6)
    ]
    projection = replace(
        make_projection(),
        participants=MappingProxyType({item.participant_id: item for item in waiting}),
    )
    app, _client, _presentation = build_app(projection=projection)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        assert len(tree._attention_since) == 5
        tree._attention_since = {
            ("p", item.participant_id): float(n) for n, item in enumerate(waiting)
        }
        tree._attention_phase = 1  # the next tick lands on the bold step
        tree._tick_attention()

        strong = [
            item.participant_id
            for item in waiting
            if _leaf(tree, item.participant_id)._attention_level == 1
        ]
        assert strong == ["participant-3", "participant-4", "participant-5"]
        assert all(_leaf(tree, item.participant_id).has_class("tree-attention") for item in waiting)


async def test_reduced_motion_keeps_the_tint_but_never_sweeps_or_breathes() -> None:
    app, _client, _presentation = build_app(reduce_motion=True)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-1")

        await _change(app, pilot, "participant-1", "awaiting_input")
        assert leaf.has_class("tree-attention")
        assert leaf._wipe is None and tree._wipe_timer is None
        assert tree._attention_timer is None


def _await_row(kind: str) -> dict:
    return {
        "kind": kind,
        "from_id": "participant-1",
        "to_id": "participant-2",
        "payload": {"token": "token", "handle": "handle"},
    }


async def test_an_await_marks_both_its_ends_until_it_finishes() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        waiter, target = _leaf(tree, "participant-1"), _leaf(tree, "participant-2")

        app._animate_bus_row(_await_row("job.await.start"))
        await wait_until(pilot, lambda: waiter._waiting is not None)
        assert target._awaited == 1
        assert render_text(waiter)[0] in REGIE_WAITING_FRAMES  # the waiting clock, not the spinner
        assert render_text(target).startswith("╴")  # the connector's spacer, pointing in

        app._animate_bus_row(_await_row("job.await.end"))
        await wait_until(pilot, lambda: waiter._waiting is None and target._awaited == 0)
        assert "╴" not in render_text(target)


async def test_a_send_that_lands_sweeps_its_target() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        target = _leaf(tree, "participant-2")

        app._animate_bus_row(_await_row("agent.send"))
        await wait_until(pilot, lambda: target._wipe is not None, timeout=5)
        assert target._wipe[1] in REGIE_ROUTE_COLORS
        assert _leaf(tree, "participant-1")._wipe is None


async def test_reduced_motion_sends_no_comet_and_flashes_nothing() -> None:
    app, _client, _presentation = build_app(reduce_motion=True)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)

        app._animate_bus_row(_await_row("agent.send"))
        await pilot.pause(0.3)
        assert not app._animation.has_active()
        assert _leaf(tree, "participant-2")._wipe is None


async def test_death_sweeps_at_twice_the_pace_of_the_other_announcements() -> None:
    from regie.ui_constants import REGIE_WIPE_DEATH_SECONDS, REGIE_WIPE_SECONDS

    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)

        await _change(app, pilot, "participant-1", "awaiting_input")
        await _change(app, pilot, "participant-2", "dead")
        needs_you = tree._wipes._sweeps[("p", "participant-1")]
        died = tree._wipes._sweeps[("p", "participant-2")]
        assert (needs_you.seconds, died.seconds) == (REGIE_WIPE_SECONDS, REGIE_WIPE_DEATH_SECONDS)
        assert REGIE_WIPE_DEATH_SECONDS <= REGIE_WIPE_SECONDS / 2
