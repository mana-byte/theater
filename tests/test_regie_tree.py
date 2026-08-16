"""Tests for the régie tree rendering.

Rendering is tested against plain dicts — the same shape the daemon returns.
What can actually be wrong here is the status glyph, the indentation of
children, whether unmanaged panes appear below the tree, and the three-row
leaf shape.
"""

from __future__ import annotations

from itertools import pairwise

from theater.regie.tree import (
    SEND_STYLE,
    cell_leaf,
    node_label,
    render_tree,
    selected_participant,
    send_path,
    shorten_path,
)

PARENT = {
    "id": "aabbccddeeff",
    "name": "Arlequin",
    "tier": "spawned",
    "harness": "vibe",
    "status": "working",
    "cwd": "/tmp/proj",
    "tmux_pane": "%1",
    "addressable": True,
    "children": [],
}

CHILD = {
    "id": "112233445566",
    "tier": "spawned",
    "harness": "claude",
    "status": "idle",
    "cwd": "/tmp/child",
    "tmux_pane": "%2",
    "addressable": True,
    "children": [],
}

UNMANAGED = {
    "pane": "%9",
    "command": "vibe",
    "harness": "vibe",
    "cwd": "/tmp/unmanaged",
    "session": "main",
    "window_name": "vibe-untagged",
}


def _rows(label) -> list[str]:
    """Split a Content label into its three rows.

    Row 0 is the spacing row, blank for a root and the incoming rail for a
    child; row 1 is the branch row; row 2 is the cwd row.
    """
    return str(label).split("\n")


def _styles(label) -> list[str]:
    """Styles carried by a Content label's spans."""
    return [span.style for span in label.spans]


def test_empty_tree_renders_no_participants():
    lines = render_tree([])
    assert len(lines) == 0


def test_single_participant_renders_harness_and_id():
    lines = render_tree([{**PARENT, "children": []}])
    assert len(lines) == 1
    rows = _rows(lines[0][0])
    assert len(rows) == 3
    assert rows[0] == ""  # blank leading row — first root, nothing above
    assert "vibe" in rows[1]
    assert "Arlequin" in rows[1]  # name, not short id
    assert "/tmp/proj" in rows[2]


def test_single_root_uses_last_branch():
    """A sole root is the last child of the invisible super-root: └──."""
    lines = render_tree([{**PARENT, "children": []}])
    rows = _rows(lines[0][0])
    assert rows[1].startswith("└── ")


def test_multiple_roots_use_branch_and_last_branch():
    """Roots are siblings under the virtual super-root, connected by rails."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    first_rows = _rows(lines[0][0])
    second_rows = _rows(lines[1][0])
    # Non-last root: ├──
    assert first_rows[1].startswith("├── ")
    # Last root: └──
    assert second_rows[1].startswith("└── ")


def test_first_root_row1_is_blank():
    """The first root has nothing above it, so row 1 stays blank."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    assert _rows(lines[0][0])[0] == ""


def test_later_root_row1_carries_rail_from_super_root():
    """A later root's row 1 shows the rail coming down from the virtual parent."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    assert _rows(lines[1][0])[0] == "│   "


def test_children_hang_off_a_branch():
    """Indentation alone could not tell a sibling from a nephew. Rails can."""
    lines = render_tree([{**PARENT, "children": [CHILD]}])
    assert len(lines) == 2
    parent_rows = _rows(lines[0][0])
    child_rows = _rows(lines[1][0])
    # Root branches off the super-root.
    assert parent_rows[1].startswith("└── ")
    # Children are one level deeper, carrying the root's continuation gap.
    assert child_rows[1].startswith("    └── ")
    assert child_rows[2].startswith("        ")


def test_only_the_last_sibling_closes_the_branch():
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    first_rows = _rows(lines[1][0])
    second_rows = _rows(lines[2][0])
    assert first_rows[1].startswith("    ├── ")
    # Row 3: continuation rail (non-last child shows the rail).
    assert first_rows[2].startswith("    │   ")
    assert second_rows[1].startswith("    └── ")
    # Row 3: continuation gap (last child).
    assert second_rows[2].startswith("        ")


def test_the_rail_continues_past_a_parent_that_has_siblings_below():
    """A grandchild under a non-last child still shows its aunt's line."""
    grandchild = {**CHILD, "id": "ddeeff001122"}
    first = {**CHILD, "children": [grandchild]}
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [first, second]}])
    first_rows = _rows(lines[1][0])
    grandchild_rows = _rows(lines[2][0])
    second_rows = _rows(lines[3][0])
    assert first_rows[1].startswith("    ├── ")
    assert grandchild_rows[1].startswith("    │   └── ")
    # Row 3: continuation = parent's rail + gap (grandchild is last child).
    assert grandchild_rows[2].startswith("    │       ")
    assert second_rows[1].startswith("    └── ")


def test_the_rail_stops_under_a_last_child():
    grandchild = {**CHILD, "id": "ddeeff001122"}
    lines = render_tree([{**PARENT, "children": [{**CHILD, "children": [grandchild]}]}])
    child_rows = _rows(lines[1][0])
    grandchild_rows = _rows(lines[2][0])
    assert child_rows[1].startswith("    └── ")
    assert grandchild_rows[1].startswith("        └── ")
    # Row 3: continuation = parent's gap + gap (both last children).
    assert grandchild_rows[2].startswith("            ")


def test_separate_roots_are_drawn_as_siblings_of_super_root():
    """Two unrelated agents branch off the invisible super-root, connected by rails."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    first_rows = _rows(lines[0][0])
    second_rows = _rows(lines[1][0])
    # Non-last root gets ├──; last root gets └──.
    assert first_rows[1].startswith("├── ")
    assert second_rows[1].startswith("└── ")


def test_row3_continuation_rail_for_middle_child_at_depth_2():
    """A non-last child at depth >= 2 shows the continuation rail on row 3.

    Row 3 must carry the continuation prefix (rail), not the branch prefix,
    so it does not look like a second node starting there.
    """
    grandchild_a = {**CHILD, "id": "ddeeff001122"}
    grandchild_b = {**CHILD, "id": "778899aabbcc"}
    parent = {**CHILD, "children": [grandchild_a, grandchild_b]}
    lines = render_tree([{**PARENT, "children": [parent]}])
    middle_rows = _rows(lines[2][0])
    # Row 2 carries the branch.
    assert middle_rows[1].startswith("        ├── ")
    # Row 3 carries the continuation rail, not the branch.
    assert middle_rows[2].startswith("        │   ")
    assert not middle_rows[2].startswith("        ├── ")


def test_row3_continuation_gap_for_last_child_at_depth_2():
    """A last child at depth >= 2 shows the continuation gap on row 3."""
    grandchild_a = {**CHILD, "id": "ddeeff001122"}
    grandchild_b = {**CHILD, "id": "778899aabbcc"}
    parent = {**CHILD, "children": [grandchild_a, grandchild_b]}
    lines = render_tree([{**PARENT, "children": [parent]}])
    last_rows = _rows(lines[3][0])
    # Row 2 carries the closing branch.
    assert last_rows[1].startswith("        └── ")
    # Row 3 carries the continuation gap, not the branch.
    assert last_rows[2].startswith("            ")
    assert not last_rows[2].startswith("        └── ")


def test_row3_root_has_continuation_gap():
    """A root participant branches off the super-root; row 3 carries the gap."""
    lines = render_tree([PARENT])
    rows = _rows(lines[0][0])
    assert rows[1].startswith("└── ")
    # Row 3: continuation gap (last child of super-root).
    assert rows[2].startswith("    ")


def test_row1_carries_the_rail_into_a_childs_branch():
    """The spacing row of a child continues the line coming from the parent.

    Leaving it blank breaks the vertical rail in the gap between every pair
    of siblings, which is what the tree drawing is for.
    """
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    assert _rows(lines[1][0])[0] == "    │   "


def test_row1_of_a_last_child_is_still_a_rail():
    """``└`` closes a line arriving from above rather than starting one.

    So a last child's spacing row is a rail like everyone else's; only its
    row 3 turns into a gap, because that is where the line actually stops.
    """
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    last_rows = _rows(lines[2][0])
    assert last_rows[0] == "    │   "
    assert last_rows[1].startswith("    └── ")
    assert last_rows[2].startswith("        ")


def test_row1_keeps_the_ancestry_of_a_grandchild():
    """A grandchild's spacing row shows its aunt's rail plus its own."""
    grandchild = {**CHILD, "id": "ddeeff001122"}
    first = {**CHILD, "children": [grandchild]}
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [first, second]}])
    assert _rows(lines[2][0])[0] == "    │   │   "


def test_first_root_row1_is_blank_later_roots_carry_rail():
    """The first root's row 1 is blank; later roots carry the super-root's rail."""
    lines = render_tree([PARENT, {**PARENT, "id": "998877665544"}])
    assert _rows(lines[0][0])[0] == ""  # first root
    assert _rows(lines[1][0])[0] == "│   "  # later root


def test_unmanaged_panes_append_after_separator():
    lines = render_tree([PARENT], unmanaged=[UNMANAGED])
    assert len(lines) == 3  # parent + separator + unmanaged
    assert "unmanaged" in str(lines[1][0])
    assert "%9" in str(lines[2][0])


def test_unmanaged_uses_command_as_harness():
    lines = render_tree([], unmanaged=[UNMANAGED])
    assert len(lines) == 2  # separator + pane
    assert "vibe" in str(lines[1][0])


def test_unmanaged_node_falls_back_to_pane_id_when_there_is_no_name():
    """Unmanaged panes are not participants; they have no name, so the
    slot shows the short id (which is the pane id) rather than nothing."""
    lines = render_tree([], unmanaged=[UNMANAGED])
    rows = _rows(lines[1][0])
    assert UNMANAGED["pane"] in rows[1]


def test_selected_participant_returns_the_node():
    lines = render_tree([{**PARENT, "children": [CHILD]}])
    node = selected_participant(lines, 0)
    assert node is not None
    assert node["id"] == "aabbccddeeff"
    node = selected_participant(lines, 1)
    assert node is not None
    assert node["id"] == "112233445566"


def test_selected_participant_on_separator_returns_none():
    lines = render_tree([PARENT], unmanaged=[UNMANAGED])
    # Index 1 is the separator
    assert selected_participant(lines, 1) is None


# ---- shorten_path --------------------------------------------------------


def test_shorten_path_deeper_than_threshold_elides_the_prefix():
    assert shorten_path("/var/a/b/c") == "…/b/c"


def test_shorten_path_exactly_at_threshold_is_unchanged():
    assert shorten_path("/b/c") == "/b/c"


def test_shorten_path_root_is_unchanged():
    assert shorten_path("/") == "/"


def test_shorten_path_home_relative_preserves_tilde_prefix():
    assert shorten_path("~/a/b/c") == "~/…/b/c"


def test_shorten_path_home_alone_is_tilde():
    assert shorten_path("~") == "~"


def test_shorten_path_none_returns_dash():
    assert shorten_path(None) == "-"


def test_shorten_path_non_default_keep():
    assert shorten_path("/a/b/c/d/e", keep=3) == "…/c/d/e"


# ---- status glyphs --------------------------------------------------------


def test_idle_status_uses_harness_icon():
    """Idle renders the harness's own icon, not a separate glyph column."""
    node = {**PARENT, "status": "idle", "harness": "claude"}
    lines = render_tree([node])
    rows = _rows(lines[0][0])
    # The harness icon for claude is ✻ (or whatever harness_icon returns).
    from theater.harness import harness_icon

    assert harness_icon("claude") in rows[1]


def test_working_status_uses_braille_spinner():
    """Working renders a braille spinner frame."""
    lines = render_tree([PARENT])  # PARENT is working
    rows = _rows(lines[0][0])
    from theater.regie.tree import _SPINNER_FRAMES

    # Row 2 is now "└── <glyph> vibe  Arlequin"; the spinner follows the branch.
    assert rows[1].split()[1] in list(_SPINNER_FRAMES)


def test_awaiting_input_status_uses_bang():
    """Awaiting input renders a bold !."""
    node = {**PARENT, "status": "awaiting_input"}
    lines = render_tree([node])
    rows = _rows(lines[0][0])
    assert "!" in rows[1]


def test_dead_status_uses_cross():
    """Dead renders ✗."""
    node = {**PARENT, "status": "dead"}
    lines = render_tree([node])
    rows = _rows(lines[0][0])
    assert "✗" in rows[1]


def test_unknown_status_uses_question_mark():
    """An unknown status renders ?."""
    node = {**PARENT, "status": "bogus"}
    lines = render_tree([node])
    rows = _rows(lines[0][0])
    assert "?" in rows[1]


def test_working_status_pulses_harness_letters_in_reverse():
    """Working leaves animate only the harness, one letter at a time."""
    from theater.regie.tree import working_harness_style

    label = node_label(PARENT, frame=0)
    pulse_styles = [style for style in _styles(label) if style.startswith("#")]
    assert pulse_styles == [working_harness_style(0, i) for i in range(len("vibe"))]

    label = node_label(PARENT, frame=1)
    pulse_styles = [style for style in _styles(label) if style.startswith("#")]
    assert pulse_styles == [working_harness_style(1, i) for i in range(len("vibe"))]
    assert pulse_styles[0] == working_harness_style(0, -1)


def test_non_working_statuses_do_not_pulse_harness_and_name_text():
    idle = node_label({**PARENT, "status": "idle"}, frame=0)
    awaiting = node_label({**PARENT, "status": "awaiting_input"}, frame=0)

    assert not any(style.startswith("#") for style in _styles(idle))
    assert not any(style.startswith("#") for style in _styles(awaiting))


# ---- cwd_segments driven by config ----------------------------------------


def test_cwd_segments_controls_path_shortening():
    """shorten_path is driven by cwd_segments, not a hardcoded 2."""
    node = {**PARENT, "cwd": "/a/b/c/d/e"}
    lines_default = render_tree([node])
    lines_keep3 = render_tree([node], cwd_segments=3)
    rows_default = _rows(lines_default[0][0])
    rows_keep3 = _rows(lines_keep3[0][0])
    # A single root's row 3 carries the continuation gap prefix.
    assert rows_default[2].lstrip() == "…/d/e"
    assert rows_keep3[2].lstrip() == "…/c/d/e"


# ---- the send path --------------------------------------------------------
#
# The route a send animation takes has to be the one the tree draws: down a
# rail, along a branch, never across the empty space between two columns. The
# fixtures are small enough that the whole expected route can be written out,
# which is a stricter check than any property could be.


def _grid(lines) -> list[str]:
    """Every rendered row of the tree, flattened — three per leaf."""
    rows: list[str] = []
    for label, *_ in lines:
        rows += _rows(label)
    return rows


def _char_at(grid: list[str], cell: tuple[int, int]) -> str:
    row, col = cell
    text = grid[row]
    return text[col] if col < len(text) else " "


def _contiguous(path: list[tuple[int, int]]) -> bool:
    """Whether every step is one cell up, down, left or right."""
    return all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in pairwise(path))


def _agent(pid: str, **kw) -> dict:
    return {**PARENT, "id": pid, "name": None, "status": "idle", "children": [], **kw}


def test_send_path_between_two_roots_runs_down_the_super_root_rail():
    """Two roots are connected only through the invisible super-root's rail.

    Written out in full: the trace leaves the sender's glyph, walks back along
    its own branch to column 0, drops down the rail the two roots share, and
    walks out along the target's branch. Any shortcut across the gap between
    columns would show up here as a missing cell.
    """
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    assert send_path(lines, "aaa", "bbb") == [
        (1, 4),  # sender's glyph
        (1, 3),
        (1, 2),
        (1, 1),
        (1, 0),  # its own ├
        (2, 0),  # the rail continuing past it
        (3, 0),  # the rail above the next root
        (4, 0),  # the target's └
        (4, 1),
        (4, 2),
        (4, 3),
        (4, 4),  # target's glyph
    ]


def test_send_path_is_the_same_route_in_reverse():
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    there = send_path(lines, "aaa", "bbb")
    back = send_path(lines, "bbb", "aaa")
    assert there is not None and back is not None
    assert back == list(reversed(there))


def test_send_path_across_two_roots_only_moves_along_drawn_rails():
    """Every cell of a root-to-root route is a character the tree drew.

    No bridging is involved between two roots, so this one can be strict:
    each cell is a rail, a branch, a dash, the space that ends a branch, or
    one of the two status glyphs at the ends.
    """
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    path = send_path(lines, "aaa", "bbb")
    assert path is not None
    assert _contiguous(path)
    drawn = {_char_at(_grid(lines), cell) for cell in path[1:-1]}
    assert drawn <= set("│├└─ ")


def test_send_path_from_a_root_to_a_grandchild_descends_each_branch():
    """A route into a subtree uses one rail column per level it goes down."""
    child = _agent("xxx", children=[_agent("ccc")])
    lines = render_tree([_agent("aaa"), _agent("bbb", children=[child])])
    path = send_path(lines, "aaa", "ccc")
    assert path is not None
    assert _contiguous(path)
    assert path[0] == (1, 4)  # the sender's glyph, a root at depth 0
    assert path[-1] == (10, 12)  # the grandchild's glyph, depth 2
    # Vertical movement only ever happens on a rail column: the super-root's
    # at 0, the child's at 4, the grandchild's at 8.
    descended = {a[1] for a, b in pairwise(path) if a[0] != b[0]}
    assert descended <= {0, 4, 8}


def test_send_path_bridges_the_cwd_row_between_a_parent_and_its_child():
    """The one cell no prefix draws: a child's rail column on the parent's cwd row.

    The parent's row 3 is only as wide as the parent's own depth, so the line
    down to its children is interrupted by the cwd text. The route crosses it
    anyway — a trace that skipped the row would appear to jump.
    """
    lines = render_tree([_agent("aaa", children=[_agent("bbb")])])
    path = send_path(lines, "aaa", "bbb")
    assert path is not None
    assert _contiguous(path)
    assert (2, 4) in path  # the parent's cwd row, at the child's rail column
    assert _char_at(_grid(lines), (2, 4)) not in "│├└─"


def test_send_path_needs_both_ends_on_screen():
    """A sender or target that is not a visible row has no route to draw."""
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    assert send_path(lines, "aaa", "nope") is None
    assert send_path(lines, "nope", "bbb") is None
    assert send_path(lines, None, "bbb") is None
    assert send_path(lines, "aaa", None) is None
    assert send_path([], "aaa", "bbb") is None


def test_send_path_to_oneself_is_not_a_route():
    lines = render_tree([_agent("aaa")])
    assert send_path(lines, "aaa", "aaa") is None


def test_send_path_ignores_unmanaged_panes():
    """Unmanaged rows are one row tall and have no rails; they are not endpoints."""
    lines = render_tree([_agent("aaa")], unmanaged=[UNMANAGED])
    assert send_path(lines, "aaa", UNMANAGED["pane"]) is None


def test_cell_leaf_splits_a_grid_row_into_leaf_and_row_within_it():
    assert cell_leaf((0, 4)) == (0, 0)
    assert cell_leaf((4, 4)) == (1, 1)
    assert cell_leaf((8, 0)) == (2, 2)


# ---- the overlay ----------------------------------------------------------


def test_overlay_draws_one_heavy_character_and_leaves_the_rest_alone():
    plain = _rows(node_label(CHILD, "    └── ", cont_prefix="        "))
    label = node_label(CHILD, "    └── ", cont_prefix="        ", overlay={(1, 4): "━"})
    marked = _rows(label)
    assert marked[1] == plain[1][:4] + "━" + plain[1][5:]
    assert len(marked[1]) == len(plain[1])
    assert marked[0] == plain[0]
    assert marked[2] == plain[2]
    assert SEND_STYLE in _styles(label)


def test_overlay_can_draw_any_of_the_three_rows():
    label = node_label(
        CHILD,
        "    └── ",
        cont_prefix="        ",
        overlay={(0, 4): "┃", (1, 5): "━", (2, 6): "┃"},
    )
    rows = _rows(label)
    assert rows[0][4] == "┃"
    assert rows[1][5] == "━"
    assert rows[2][6] == "┃"
    assert _styles(label).count(SEND_STYLE) == 3


def test_overlay_past_the_end_of_a_row_is_padded():
    """A trace crossing spacer cells must not disappear for a frame."""
    rows = _rows(node_label(CHILD, "    └── ", cont_prefix="    ", overlay={(0, 10): "┃"}))
    assert rows[0][10] == "┃"


def test_no_overlay_renders_exactly_as_before():
    assert str(node_label(PARENT, "└── ", cont_prefix="    ")) == str(
        node_label(PARENT, "└── ", cont_prefix="    ", overlay=None)
    )
    assert str(node_label(PARENT, "└── ", cont_prefix="    ", overlay={})) == str(
        node_label(PARENT, "└── ", cont_prefix="    ")
    )


def test_overlay_keeps_the_styles_of_the_text_it_splits():
    """Drawing a trace mid-run must not flatten the run's own style."""
    label = node_label(PARENT, "└── ", cont_prefix="    ", frame=0, overlay={(2, 6): "━"})
    assert "$text dim" in _styles(label)

    assert SEND_STYLE in _styles(label)
