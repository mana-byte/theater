"""The row-two layout is the source of the rendered name and click spans."""

from __future__ import annotations

from regie.render.glyphs import node_label
from regie.render.row2 import row2_layout
from rich.cells import cell_len

from packages.regie.tests.support.tree_nodes import CHILD


def test_fold_and_name_spans_match_the_drawn_row_at_a_narrow_width():
    node = {
        **CHILD,
        "name": "row-two-name-very-long",
        "status": "working",
        "hidden_count": 4,
        "hidden_states": {"dead": 1, "working": 3},
    }
    layout = row2_layout(node, "├── ", width=25, frame=3, show_harness=False, fold_hint=True)
    row = node_label(node, "├── ", frame=3, layout=layout).plain.splitlines()[1]

    assert layout.name_text == "row-two-nam…"
    assert layout.name_span == (cell_len(row.split(layout.name_text)[0]), 19)
    assert layout.fold_span == (19, cell_len(row))
    assert row[layout.name_span[0] : layout.name_span[1]] == layout.name_text


def test_reveal_counts_codepoints_but_the_name_span_counts_wide_cells():
    node = {**CHILD, "name": "界界", "status": "idle"}
    layout = row2_layout(node, "├── ", reveal=8, show_harness=False)
    row = node_label(node, "├── ", layout=layout).plain.splitlines()[1]

    assert row.endswith("界")
    assert layout.name_span == (cell_len(row[:-1]), cell_len(row)) == (7, 9)
    assert layout.fold_span is None


def test_await_clock_keeps_legacy_room_for_a_zero_width_base_icon():
    node = {**CHILD, "name": "medium", "status": "idle", "icon": "\u0301", "children": [{}]}
    layout = row2_layout(
        node,
        "├── ",
        width=12,
        waiting="$accent",
        show_harness=False,
        fold_hint=True,
        focused=True,
        marquee_offset=3,
    )

    assert layout.name_room == 3
    assert layout.name_span == (6, 9) and layout.fold_span == (9, 12)
    assert node_label(node, "├── ", layout=layout).plain.splitlines()[1] == "├── ◴  med  "
