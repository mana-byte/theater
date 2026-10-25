"""Folding an agent's children: keys, clicks, what a fold shows, and that it persists."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from regie.tree_layout import TreeLayout
from regie.widgets import ParticipantTree
from regie.widgets.leaf import AgentLeaf

from packages.regie.tests.support.app_fakes import build_app, make_participant, make_projection
from packages.regie.tests.support.waiting import wait_until


def _family(statuses: dict[str, str] | None = None):
    """root -> (builder -> tester, reviewer), plus an unrelated other."""
    spec = {
        "participant-1": ("orchestrator", None),
        "participant-2": ("builder", "participant-1"),
        "participant-3": ("tester", "participant-2"),
        "participant-4": ("reviewer", "participant-1"),
        "participant-5": ("other", None),
    }
    people = [
        make_participant(
            pid,
            name=name,
            parent_id=parent,
            status=(statuses or {}).get(pid, "idle"),
            cwd=f"/p/{name}",
        )
        for pid, (name, parent) in spec.items()
    ]
    return replace(
        make_projection(),
        participants=MappingProxyType({item.participant_id: item for item in people}),
    )


def _leaf(tree: ParticipantTree, participant_id: str) -> AgentLeaf:
    leaf = tree._key_widgets[("p", participant_id)]
    assert isinstance(leaf, AgentLeaf)
    return leaf


def _row(leaf: AgentLeaf) -> str:
    return leaf.render_line(1).text.rstrip()


async def test_space_folds_the_selected_agents_children_and_brings_them_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "layout.json"
    app, _client, _presentation = build_app(projection=_family(), tree_layout_path=path)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        tree.select("participant-1")

        await pilot.press("space")
        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1", "participant-5"))
        assert tree.selected_participant_id == "participant-1"  # the folded row keeps the cursor
        assert "▸3" in _row(_leaf(tree, "participant-1"))
        assert json.loads(path.read_text())["folded"] == ["participant-1"]

        await pilot.press("space")
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        assert "folded" not in json.loads(path.read_text())


async def test_space_on_an_agent_with_nothing_below_it_does_nothing(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    app, _client, _presentation = build_app(projection=_family(), tree_layout_path=path)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        tree.select("participant-5")
        await pilot.press("space")
        await pilot.pause(0.2)
        assert len(tree.participant_ids) == 5 and not path.exists()


async def test_the_chevron_shows_on_the_focused_row_and_a_click_on_it_folds(
    tmp_path: Path,
) -> None:
    app, _client, _presentation = build_app(
        projection=_family(), tree_layout_path=tmp_path / "layout.json"
    )

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        boss, other = _leaf(tree, "participant-1"), _leaf(tree, "participant-5")
        tree.select("participant-5")
        await wait_until(pilot, lambda: "▾" not in _row(boss))  # not in focus: no chevron
        await pilot.hover(boss)
        await wait_until(pilot, lambda: "▾" in _row(boss))
        assert "▾" not in _row(other)  # and never on a row with nothing below it

        span = boss._fold_span()
        assert span is not None
        await pilot.click(boss, offset=(span[0] + 3, 1))  # inside the badge, past the padding
        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1", "participant-5"))


async def test_a_folded_row_shows_trouble_below_it_and_the_cursor_can_jump_to_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "layout.json"
    TreeLayout(folded={"participant-1"}).save(path)
    app, _client, _presentation = build_app(projection=_family(), tree_layout_path=path)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1", "participant-5"))
        boss = _leaf(tree, "participant-1")
        assert "▸3" in _row(boss) and "!" not in _row(boss)

        projection = _family({"participant-3": "awaiting_input"})  # a hidden grandchild needs you
        app._state.projection = projection
        app._show_projection(projection)
        await wait_until(pilot, lambda: "▸3 !" in _row(boss))
        assert boss._wipe is not None or tree._wipes.has(("p", "participant-1"))  # it sweeps

        tree.select("participant-5")
        await pilot.press("]")
        assert tree.selected_participant_id == "participant-1"  # the fold is where trouble is


async def test_a_staged_agent_under_a_fold_marks_the_row_that_stands_for_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "layout.json"
    TreeLayout(folded={"participant-1"}).save(path)
    app, _client, _presentation = build_app(projection=_family(), tree_layout_path=path)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1", "participant-5"))
        tree.mark_surfaces(staged_id="participant-3", trajectory_id=None)
        assert _leaf(tree, "participant-1").has_class("tree-staged")
        assert not _leaf(tree, "participant-5").has_class("tree-staged")


async def test_a_working_agent_under_a_fold_keeps_the_badge_turning(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    TreeLayout(folded={"participant-1"}).save(path)
    app, _client, _presentation = build_app(
        projection=_family({"participant-4": "working"}), tree_layout_path=path
    )

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1", "participant-5"))
        boss = _leaf(tree, "participant-1")
        await wait_until(pilot, lambda: boss._spinner_sub is not None)
        seen = {_row(boss)}
        await wait_until(pilot, lambda: seen.add(_row(boss)) or len(seen) > 2, timeout=5)


async def test_folds_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    app, _client, _presentation = build_app(projection=_family(), tree_layout_path=path)
    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        tree.select("participant-2")
        await pilot.press("space")
        await wait_until(
            pilot, lambda: "participant-3" not in tree.participant_ids
        )  # builder folded its tester

    again, _client, _presentation = build_app(projection=_family(), tree_layout_path=path)
    async with again.run_test() as pilot:
        tree = again.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 4)
        assert "participant-3" not in tree.participant_ids
        assert "▸1" in _row(_leaf(tree, "participant-2"))
