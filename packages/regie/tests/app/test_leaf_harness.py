"""The harness is spelled out on a row only while the row is in focus."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from regie.widgets import ParticipantTree
from textual import events

from packages.regie.tests.support.app_fakes import build_app, make_participant, make_projection
from packages.regie.tests.support.tree_rows import leaf as _leaf
from packages.regie.tests.support.tree_rows import row as _row
from packages.regie.tests.support.tree_rows import rows as _rows
from packages.regie.tests.support.waiting import wait_until


async def test_the_harness_comes_back_with_the_cursor_and_with_the_pointer() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        first, second = _leaf(tree, "participant-1"), _leaf(tree, "participant-2")

        await wait_until(pilot, lambda: "codex" in _row(first))  # the cursor is on it
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


# ---- a long name -------------------------------------------------------------------------------

LONG = "a-really-quite-long-participant-name-that-keeps-going"


def _long_name_app():
    people = [
        make_participant("participant-1", name="short", cwd="/Users/me/proj"),
        make_participant(
            "participant-2", name=LONG, cwd="/Users/me/proj/api", parent_id="participant-1"
        ),
    ]
    projection = replace(
        make_projection(),
        participants=MappingProxyType({item.participant_id: item for item in people}),
    )
    return build_app(projection=projection)


async def test_a_long_name_is_cut_with_an_ellipsis_and_never_hides_the_directory() -> None:
    app, _client, _presentation = _long_name_app()

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        long = _leaf(tree, "participant-2")
        await wait_until(pilot, lambda: "…" in _rows(long)[1])

        first, second, third = _rows(long)
        assert first.strip() == "│   │" and second.endswith("…")  # one line, ending in an ellipsis
        assert third.strip().endswith("…/proj/api")  # and the directory is where it belongs
        assert long._marquee_sub is None  # nothing scrolls on a row that is not in focus


async def test_the_long_name_scrolls_on_the_focused_row_and_settles_when_it_leaves() -> None:
    app, _client, _presentation = _long_name_app()

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        long = _leaf(tree, "participant-2")
        tree.select("participant-2")
        await wait_until(pilot, lambda: long._marquee_sub is not None)

        seen = {_rows(long)[1]}
        await wait_until(pilot, lambda: seen.add(_rows(long)[1]) or len(seen) >= 3, timeout=10)
        assert all("codex" in row for row in seen)  # the harness is back on the focused row
        assert all(_rows(long)[2].strip().endswith("…/proj/api") for _ in seen)

        tree.select("participant-1")
        await wait_until(pilot, lambda: long._marquee_sub is None)
        assert _rows(long)[1].endswith("…") and long._marquee_offset == 0


async def test_a_name_that_fits_never_starts_a_scroll() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        tree.select("participant-2")
        leaf = _leaf(tree, "participant-2")
        await wait_until(
            pilot,
            lambda: tree.selected_participant_id == "participant-2" and "codex" in _row(leaf),
        )
        assert leaf._marquee_sub is None


async def test_renaming_a_long_name_keeps_one_line_and_the_editor_inside_the_row() -> None:
    app, _client, _presentation = _long_name_app()

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 2)
        long = _leaf(tree, "participant-2")
        tree.select("participant-2")
        await long.begin_rename()
        editor = long._name_editor
        assert editor is not None
        await wait_until(
            pilot, lambda: editor.styles.max_width is not None and "…" not in _rows(long)[1]
        )

        rows = _rows(long)
        assert "…" not in rows[1] and rows[2].strip().endswith("…/proj/api")
        span = long._name_span()
        assert span is not None and span[1] <= long.content_size.width
        assert editor.styles.max_width is not None


# ---- the cost on row three ---------------------------------------------------------------------


async def test_a_focused_folded_row_still_shows_its_cost_on_row_three(tmp_path) -> None:
    from regie.tree_layout import TreeLayout

    people = [
        make_participant("participant-1", name=LONG, cwd="/Users/me/proj"),
        make_participant("participant-2", name="kid", parent_id="participant-1", status="working"),
        make_participant("participant-3", name="kin", parent_id="participant-1"),
    ]
    projection = replace(
        make_projection(),
        participants=MappingProxyType({item.participant_id: item for item in people}),
    )
    path = tmp_path / "layout.json"
    TreeLayout(folded={"participant-1"}).save(path)
    app, _client, _presentation = build_app(projection=projection, tree_layout_path=path)

    async with app.run_test(size=(52, 30)) as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: tree.participant_ids == ("participant-1",))
        leaf = _leaf(tree, "participant-1")
        tree.select("participant-1")  # focused: harness, fold badge, cost, all competing
        await wait_until(pilot, lambda: "$0.42" in _rows(leaf)[2], timeout=8)  # it counts up

        _, second, third = _rows(leaf)
        assert "$" not in second and "▸2" in second  # row two is identity and state
        assert third.endswith("$0.42")  # at the right edge of row three
        assert "…/me/proj" in third  # and the directory is still there beside it
