"""Folding an agent's children: keys, clicks, what a fold shows, and that it persists."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from regie.tree_layout import TreeLayout
from regie.widgets import ParticipantTree

from packages.regie.tests.support.app_fakes import build_app, make_participant, make_projection
from packages.regie.tests.support.tree_rows import leaf as _leaf
from packages.regie.tests.support.tree_rows import row as _row
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
        ticks: list[bool] = []
        app.set_timer(0.05, lambda: ticks.append(True))
        await wait_until(pilot, lambda: bool(ticks))
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
        await wait_until(pilot, lambda: "▸3" in _row(boss))
        assert "!" not in _row(boss)

        projection = _family({"participant-3": "awaiting_input"})  # a hidden grandchild needs you
        app._state.projection = projection
        app._show_projection(projection)
        await wait_until(pilot, lambda: "▸3 !1" in _row(boss))
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
        await wait_until(pilot, lambda: "▸1" in _row(_leaf(tree, "participant-2")))


async def test_an_await_on_a_folded_agent_still_changes_the_waiters_glyph(tmp_path: Path) -> None:
    from regie.ui_constants import REGIE_WAITING_FRAMES

    path = tmp_path / "layout.json"
    TreeLayout(folded={"participant-1"}).save(path)
    app, _client, _presentation = build_app(projection=_family(), tree_layout_path=path)

    def event(kind: str) -> dict:
        return {
            "kind": kind,
            "from_id": "participant-5",
            "to_id": "participant-3",  # folded away under participant-1
            "payload": {"token": "t", "handle": "h"},
        }

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1", "participant-5"))
        waiter, boss = _leaf(tree, "participant-5"), _leaf(tree, "participant-1")

        app._animate_bus_row(event("job.await.start"))
        await wait_until(pilot, lambda: waiter._waiting is not None)
        await wait_until(pilot, lambda: _row(waiter).lstrip("└├│─ ")[:1] in REGIE_WAITING_FRAMES)
        assert _row(waiter).lstrip("└├│─ ")[0] in REGIE_WAITING_FRAMES  # the waiting glyph
        assert boss._awaited  # the fold that hides the target says it is being waited on

        tree.select("participant-1")  # unfold: the marks follow the target out of the fold
        await pilot.press("space")
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        target = _leaf(tree, "participant-3")
        await wait_until(pilot, lambda: target._awaited and not boss._awaited)

        app._animate_bus_row(event("job.await.end"))
        await wait_until(pilot, lambda: waiter._waiting is None and not target._awaited)


async def test_a_parent_awaiting_its_folded_child_shows_the_waiting_glyph_and_keeps_it_on_unfold(
    tmp_path: Path,
) -> None:
    from regie.ui_constants import REGIE_WAITING_FRAMES

    path = tmp_path / "layout.json"
    TreeLayout(folded={"participant-1"}).save(path)
    app, _client, _presentation = build_app(projection=_family(), tree_layout_path=path)

    def event(kind: str) -> dict:
        return {
            "kind": kind,
            "from_id": "participant-1",  # the parent, on screen
            "to_id": "participant-4",  # its own child, folded under it
            "payload": {"token": "t", "handle": "h"},
        }

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1", "participant-5"))
        parent = _leaf(tree, "participant-1")

        app._animate_bus_row(event("job.await.start"))
        await wait_until(pilot, lambda: parent._waiting is not None)
        await wait_until(
            pilot, lambda: _row(parent).lstrip("└├│─┢┡┠━┃┗┏ ")[:1] in REGIE_WAITING_FRAMES
        )
        assert _row(parent).lstrip("└├│─┢┡┠━┃┗┏ ")[0] in REGIE_WAITING_FRAMES

        tree.select("participant-1")
        await pilot.press("space")  # unfold: the wait must still be there
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        child = _leaf(tree, "participant-4")
        await wait_until(pilot, lambda: child._awaited)
        assert parent._waiting is not None

        app._animate_bus_row(event("job.await.end"))
        await wait_until(pilot, lambda: parent._waiting is None and not child._awaited)
