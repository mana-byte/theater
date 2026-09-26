from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from regie.paths import RegiePaths
from regie.tree_layout import TreeLayout
from regie.widgets import ParticipantTree
from regie.widgets.name_editor import NameEditor
from regie.widgets.separator import SeparatorRow
from textual.widgets import Input

from packages.regie.tests.test_ui import _app, _participant, _projection
from tests.rig.waiting import wait_until


async def test_jk_reorders_roots_and_stays_out_of_the_usage_footer(tmp_path: Path) -> None:
    path = tmp_path / "tree-layout.json"
    app, _client, _presentation = _app(tree_layout_path=path)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree.select("participant-1")
        await pilot.press("shift+j")
        await wait_until(
            pilot,
            lambda: tree.participant_ids == ("participant-2", "participant-1"),
        )
        assert tree.selected_key == ("p", "participant-1")

        await pilot.press("j")
        assert app._usage_panel.in_footer
        await pilot.press("shift+k", "minus")
        assert tree.participant_ids == ("participant-2", "participant-1")
        assert not list(app.screen.query(Input))


async def test_jk_on_a_child_stays_within_its_parent(tmp_path: Path) -> None:
    root = _participant("participant-1", name="root")
    first = _participant("participant-2", name="first", parent_id=root.participant_id)
    second = _participant("participant-3", name="second", parent_id=root.participant_id)
    other = _participant("participant-4", name="other")
    projection = replace(
        _projection(),
        participants=MappingProxyType(
            {item.participant_id: item for item in (root, first, second, other)}
        ),
    )
    app, _client, _presentation = _app(
        tree_layout_path=tmp_path / "tree-layout.json",
        projection=projection,
    )

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 4)
        tree.select(second.participant_id)
        await pilot.press("shift+k")
        await wait_until(
            pilot,
            lambda: (
                tree.participant_ids
                == (
                    root.participant_id,
                    second.participant_id,
                    first.participant_id,
                    other.participant_id,
                )
            ),
        )
        assert tree.selected_key == ("p", second.participant_id)


async def test_add_separator_revalidates_then_persists_across_reload(tmp_path: Path) -> None:
    path = tmp_path / "tree-layout.json"
    app, _client, _presentation = _app(tree_layout_path=path)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree.select("participant-2")
        await pilot.press("minus")
        await wait_until(pilot, lambda: bool(app.screen.query(Input)))
        app._state.projection = replace(
            app._state.projection,
            participants=MappingProxyType(
                {"participant-1": app._state.projection.participants["participant-1"]}
            ),
        )
        app.screen.query_one(Input).value = "Stale"
        await pilot.press("enter")
        await pilot.pause()
        assert not app._tree_layout.separators

        app._state.projection = _projection()
        app._show_projection(app._state.projection)
        tree.select("participant-2")
        await pilot.press("minus")
        await wait_until(pilot, lambda: bool(app.screen.query(Input)))
        app.screen.query_one(Input).value = "Backend"
        await pilot.press("enter")
        await wait_until(
            pilot, lambda: tree.selected_key is not None and tree.selected_key[0] == "s"
        )
        separator_key = tree.selected_key
        assert separator_key is not None
        widget = tree._key_widgets[separator_key]
        assert isinstance(widget, SeparatorRow)
        assert "Backend" in str(widget.render())

        assert widget.size.height == 2
        row = widget.render_line(1).text
        assert "─" not in row  # only the name, no horizontal bar
        width = widget.content_size.width
        name_at = row.index("Backend")
        assert abs(name_at - (width - name_at - len("Backend"))) <= 4  # centred

        renamed = "Backend services"
        await pilot.press("r")
        await wait_until(pilot, lambda: bool(widget.query(NameEditor)))
        await pilot.press(*renamed, "enter")  # the editor opens with the name selected
        await wait_until(pilot, lambda: renamed in widget.render_line(1).text)

    loaded, warning = TreeLayout.load(path)
    assert warning is None
    assert loaded.separators[separator_key[1]] == {"name": renamed}

    fresh, _client, _presentation = _app(tree_layout_path=path)
    async with fresh.run_test() as pilot:
        tree = fresh.query_one(ParticipantTree)
        await wait_until(pilot, lambda: separator_key in tree.selectable_keys)


async def test_separator_actions_do_not_control_participants_and_x_deletes(tmp_path: Path) -> None:
    path = RegiePaths(tmp_path).tree_layout_path
    separator_id = "sep:1234abcd"
    TreeLayout(
        orders={"": [separator_id, "participant-1", "participant-2"]},
        separators={separator_id: {"name": "Backend"}},
    ).save(path)
    app, client, presentation = _app(tree_layout_path=path)

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        separator_key = ("s", separator_id)
        await wait_until(pilot, lambda: separator_key in tree.selectable_keys)
        tree.select_key(separator_key)
        await pilot.press("s", "i", "f", "g", "enter", "h", "l")
        await pilot.pause()
        assert client.controls.requests == []
        assert client.participants.terminated == []
        assert presentation.staged == []

        await pilot.press("x")
        await wait_until(pilot, lambda: separator_key not in tree.selectable_keys)
        assert client.participants.terminated == []

    loaded, warning = TreeLayout.load(path)
    assert warning is None
    assert separator_id not in loaded.separators
