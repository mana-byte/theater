"""The options menu drawn as the tree's last root: rails, rows and heights."""

from __future__ import annotations

from regie.menu import Loaded, MenuItem, MenuView, menu_nodes
from regie.render.layout import render_tree, row_count
from regie.render.menu import menu_label
from regie.render.routing import section_arms

from packages.regie.tests.support.tree_nodes import PARENT
from packages.regie.tests.support.tree_nodes import label_rows as _label_rows

ROOT = ("g", "options")


def label_rows(label) -> list[str]:
    return [row.rstrip() for row in _label_rows(label)]


def _agent(pid: str, **kw) -> dict:
    return {**PARENT, "id": pid, "name": None, "status": "idle", "children": [], **kw}


def _items() -> tuple[MenuItem, ...]:
    return (
        MenuItem(
            "spawn",
            "Spawn",
            "start a session",
            choices=Loaded((MenuItem("codex", "codex", "pick a directory", glyph="◈"),)),
        ),
        MenuItem("bus", "Show bus panel", "coordination traffic"),
    )


def _lines(*, open: bool, expanded=()):
    menu = menu_nodes(_items(), MenuView(expanded=frozenset(expanded)), open=open)
    return render_tree([_agent("aaa"), _agent("bbb")], menu=menu)


def test_folded_the_menu_is_one_line_and_the_last_agent_stops_being_last():
    lines = _lines(open=False)

    assert [key for _, _, key, _, _ in lines][-1] == ROOT
    assert [prefix for _, _, _, prefix, _ in lines] == ["├── ", "├── ", "└── "]
    assert label_rows(lines[-1][0]) == ["└── + options · ctrl+p"]
    assert row_count(ROOT, lines[-1][1]) == 1


def test_open_it_is_three_rows_with_a_hint_and_its_options_hang_from_it():
    lines = _lines(open=True)

    assert row_count(ROOT, lines[2][1]) == 3
    assert label_rows(lines[2][0]) == ["│", "└── + options · ctrl+p", "    x closes · / filters"]
    assert [(key, prefix) for _, _, key, prefix, _ in lines[3:]] == [
        (("m", "spawn"), "    ├── "),
        (("m", "bus"), "    └── "),
    ]


def test_an_option_is_a_rail_a_title_and_its_help_under_the_title():
    lines = _lines(open=True)

    assert label_rows(lines[3][0]) == ["    │", "    ├── ▸ Spawn", "    │     start a session"]
    assert label_rows(lines[4][0]) == [
        "    │",
        "    └── · Show bus panel",
        "          coordination traffic",
    ]


def test_a_choice_hangs_one_level_deeper_with_its_own_glyph():
    lines = _lines(open=True, expanded=("spawn",))

    keys = [key for _, _, key, _, _ in lines]
    assert keys[-3:] == [("m", "spawn"), ("m", "spawn/codex"), ("m", "bus")]
    assert label_rows(lines[4][0])[1] == "    │   └── ◈ codex"


def test_a_section_stops_at_the_menu_so_its_heavy_rails_never_run_into_it():
    lines = _lines(open=False)
    separator = ("s", "sep")
    lines[0:0] = render_tree(
        [{"id": "sep", "kind": "separator", "name": "X", "collapsed": False, "children": []}]
    )

    arms = section_arms(lines, separator)

    assert ROOT not in arms


def test_the_menu_of_an_empty_tree_is_the_first_root_and_draws_no_rail_above_it():
    menu = menu_nodes(_items(), MenuView(), open=True)
    lines = render_tree([], menu=menu)

    assert lines[0][2] == ROOT
    assert label_rows(lines[0][0])[0] == ""


def test_the_roots_third_row_shows_the_filter_or_leaves_its_place_to_the_editor():
    menu = menu_nodes(_items(), MenuView(query="bus"), open=True)
    lines = render_tree([_agent("aaa")], menu=menu)

    assert label_rows(lines[1][0])[2] == "    / bus"
    editing = menu_label(lines[1][1], "└── ", "    ", editing=True)
    assert label_rows(editing)[2] == "    /"


def test_a_long_help_is_cut_with_an_ellipsis_until_it_scrolls_in_a_window():
    node = {**menu_nodes((MenuItem("a", "A", "word " * 20),), MenuView(), open=True)["children"][0]}
    cut = menu_label(node, "└── ", "    ", width=40)
    moving = [menu_label(node, "└── ", "    ", width=40, help_offset=n) for n in (0, 12, 13)]

    assert label_rows(cut)[2].endswith("…") and len(label_rows(cut)[2]) <= 40
    third = [label_rows(label)[2] for label in moving]
    assert third[0] != third[2] and all("…" not in row for row in third)  # a window, not a cut


def test_an_option_wears_its_shortcut_after_the_title_and_cuts_the_title_to_keep_it():
    item = MenuItem("bus", "Show the bus panel", "traffic", shortcut="v")
    node = menu_nodes((item,), MenuView(), open=True)["children"][0]

    assert (
        label_rows(menu_label(node, "    └── ", "        "))[1]
        == "    └── · Show the bus panel · v"
    )
    narrow = label_rows(menu_label(node, "    └── ", "        ", width=24))[1]
    assert narrow.endswith("… · v") and len(narrow) <= 24
