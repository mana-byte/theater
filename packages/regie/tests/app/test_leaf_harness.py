"""The harness is spelled out on a row only while the row is in focus."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from regie.widgets import ParticipantTree
from regie.widgets.leaf import AgentLeaf
from textual import events

from packages.regie.tests.support.app_fakes import build_app, make_participant, make_projection
from packages.regie.tests.support.waiting import wait_until


def _leaf(tree: ParticipantTree, participant_id: str) -> AgentLeaf:
    leaf = tree._key_widgets[("p", participant_id)]
    assert isinstance(leaf, AgentLeaf)
    return leaf


def _row(leaf: AgentLeaf) -> str:
    return leaf.render_line(1).text


async def test_the_harness_comes_back_with_the_cursor_and_with_the_pointer() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        first, second = _leaf(tree, "participant-1"), _leaf(tree, "participant-2")

        assert "codex" in _row(first)  # the cursor is on it
        assert "codex" not in _row(second) and "second" in _row(second)

        tree.select("participant-2")
        await wait_until(pilot, lambda: "codex" in _row(second))
        assert "codex" not in _row(first)

        await pilot.hover(first)
        await wait_until(pilot, lambda: "codex" in _row(first))
        await pilot.hover(second)
        await wait_until(pilot, lambda: "codex" not in _row(first))


async def test_a_row_without_a_name_always_shows_its_harness() -> None:
    nameless = make_participant("participant-3", name=None)  # type: ignore[arg-type]
    projection = replace(
        make_projection(),
        participants=MappingProxyType(
            {**make_projection().participants, nameless.participant_id: nameless}
        ),
    )
    app, _client, _presentation = build_app(projection=projection)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 3)
        assert "codex" in _row(_leaf(tree, "participant-3"))  # not selected, not hovered


async def test_a_click_on_the_name_is_measured_before_selecting_moves_it(monkeypatch) -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        leaf = _leaf(tree, "participant-2")
        harness_when_measured: list[bool] = []

        def measure(_event: events.Click) -> bool:
            harness_when_measured.append(leaf._harness_shown())
            return False

        monkeypatch.setattr(leaf, "_name_clicked", measure)
        await leaf._on_click(events.Click(leaf, 5, 1, 0, 0, 1, False, False, False))

        assert harness_when_measured == [False]  # the layout the human clicked on
        await wait_until(pilot, leaf._harness_shown)  # and only then it moved


async def test_the_rename_editor_follows_the_name_when_the_harness_comes_or_goes() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        first = _leaf(tree, "participant-1")
        await first.begin_rename()
        editor = first._name_editor
        assert editor is not None
        focused_x = editor.styles.offset.x.value

        first._cursor_selected = False  # the cursor leaves while the editor is still open
        first._hovered = False
        first.update(first._render_label(), layout=False)
        first._sync_rename_geometry()
        assert editor.styles.offset.x.value < focused_x  # the name, and the editor, moved left
