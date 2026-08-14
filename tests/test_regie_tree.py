"""Tests for the régie tree rendering.

Rendering is tested against plain dicts — the same shape the daemon returns.
What can actually be wrong here is the status glyph, the indentation of
children, whether unmanaged panes appear below the tree, and the three-row
leaf shape.
"""

from __future__ import annotations

from theater.regie.tree import render_tree, selected_participant, shorten_path

PARENT = {
    "id": "aabbccddeeff",
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


def test_empty_tree_renders_no_participants():
    lines = render_tree([])
    assert len(lines) == 0


def test_single_participant_renders_harness_and_id():
    lines = render_tree([{**PARENT, "children": []}])
    assert len(lines) == 1
    rows = _rows(lines[0][0])
    assert len(rows) == 3
    assert rows[0] == ""  # blank leading row
    assert "vibe" in rows[1]
    assert "aabbccdd" in rows[1]  # short id
    assert "/tmp/proj" in rows[2]


def test_children_hang_off_a_branch():
    """Indentation alone could not tell a sibling from a nephew. Rails can."""
    lines = render_tree([{**PARENT, "children": [CHILD]}])
    assert len(lines) == 2
    parent_rows = _rows(lines[0][0])
    child_rows = _rows(lines[1][0])
    # Roots have no branch of their own.
    assert not parent_rows[1].startswith(("├", "└", " "))
    # Children get a branch on row 2; row 3 carries the continuation gap.
    assert child_rows[1].startswith("└── ")
    assert child_rows[2].startswith("    ")


def test_only_the_last_sibling_closes_the_branch():
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    first_rows = _rows(lines[1][0])
    second_rows = _rows(lines[2][0])
    assert first_rows[1].startswith("├── ")
    # Row 3: continuation rail (non-last child shows the rail).
    assert first_rows[2].startswith("│   ")
    assert second_rows[1].startswith("└── ")
    # Row 3: continuation gap (last child).
    assert second_rows[2].startswith("    ")


def test_the_rail_continues_past_a_parent_that_has_siblings_below():
    """A grandchild under a non-last child still shows its aunt's line."""
    grandchild = {**CHILD, "id": "ddeeff001122"}
    first = {**CHILD, "children": [grandchild]}
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [first, second]}])
    first_rows = _rows(lines[1][0])
    grandchild_rows = _rows(lines[2][0])
    second_rows = _rows(lines[3][0])
    assert first_rows[1].startswith("├── ")
    assert grandchild_rows[1].startswith("│   └── ")
    # Row 3: continuation = parent's rail + gap (grandchild is last child).
    assert grandchild_rows[2].startswith("│       ")
    assert second_rows[1].startswith("└── ")


def test_the_rail_stops_under_a_last_child():
    grandchild = {**CHILD, "id": "ddeeff001122"}
    lines = render_tree([{**PARENT, "children": [{**CHILD, "children": [grandchild]}]}])
    child_rows = _rows(lines[1][0])
    grandchild_rows = _rows(lines[2][0])
    assert child_rows[1].startswith("└── ")
    assert grandchild_rows[1].startswith("    └── ")
    # Row 3: continuation = parent's gap + gap (both last children).
    assert grandchild_rows[2].startswith("        ")


def test_separate_roots_are_not_drawn_as_siblings():
    """Two unrelated agents are not children of anything, so no rails."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    assert all(
        not _rows(line[0])[1].startswith(("├", "└")) for line in lines
    )


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
    assert middle_rows[1].startswith("    ├── ")
    # Row 3 carries the continuation rail, not the branch.
    assert middle_rows[2].startswith("    │   ")
    assert not middle_rows[2].startswith("    ├── ")


def test_row3_continuation_gap_for_last_child_at_depth_2():
    """A last child at depth >= 2 shows the continuation gap on row 3."""
    grandchild_a = {**CHILD, "id": "ddeeff001122"}
    grandchild_b = {**CHILD, "id": "778899aabbcc"}
    parent = {**CHILD, "children": [grandchild_a, grandchild_b]}
    lines = render_tree([{**PARENT, "children": [parent]}])
    last_rows = _rows(lines[3][0])
    # Row 2 carries the closing branch.
    assert last_rows[1].startswith("    └── ")
    # Row 3 carries the continuation gap, not the branch.
    assert last_rows[2].startswith("        ")
    assert not last_rows[2].startswith("    └── ")


def test_row3_no_rail_for_root():
    """A root participant has no rail on any row."""
    lines = render_tree([PARENT])
    rows = _rows(lines[0][0])
    assert not rows[1].startswith(("├", "└", "│"))
    assert not rows[2].startswith(("├", "└", "│"))


def test_row1_carries_the_rail_into_a_childs_branch():
    """The spacing row of a child continues the line coming from the parent.

    Leaving it blank breaks the vertical rail in the gap between every pair
    of siblings, which is what the tree drawing is for.
    """
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    assert _rows(lines[1][0])[0] == "│   "


def test_row1_of_a_last_child_is_still_a_rail():
    """``└`` closes a line arriving from above rather than starting one.

    So a last child's spacing row is a rail like everyone else's; only its
    row 3 turns into a gap, because that is where the line actually stops.
    """
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    last_rows = _rows(lines[2][0])
    assert last_rows[0] == "│   "
    assert last_rows[1].startswith("└── ")
    assert last_rows[2].startswith("    ")


def test_row1_keeps_the_ancestry_of_a_grandchild():
    """A grandchild's spacing row shows its aunt's rail plus its own."""
    grandchild = {**CHILD, "id": "ddeeff001122"}
    first = {**CHILD, "children": [grandchild]}
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [first, second]}])
    assert _rows(lines[2][0])[0] == "│   │   "


def test_row1_of_a_root_stays_blank():
    """A root has no branch, so there is no line above it to continue."""
    lines = render_tree([PARENT, {**PARENT, "id": "998877665544"}])
    assert all(_rows(line[0])[0] == "" for line in lines)


def test_unmanaged_panes_append_after_separator():
    lines = render_tree([PARENT], unmanaged=[UNMANAGED])
    assert len(lines) == 3  # parent + separator + unmanaged
    assert "unmanaged" in str(lines[1][0])
    assert "%9" in str(lines[2][0])


def test_unmanaged_uses_command_as_harness():
    lines = render_tree([], unmanaged=[UNMANAGED])
    assert len(lines) == 2  # separator + pane
    assert "vibe" in str(lines[1][0])


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


def test_unaddressable_id_renders_with_no_star():
    """The * reach mark is gone; the id is styled dim italic instead."""
    ext = {**PARENT, "tier": "external", "addressable": False}
    lines = render_tree([ext])
    label = lines[0][0]
    assert "*" not in str(label)
    # The id is still present in plain text.
    assert "aabbccdd" in str(label)


# ---- shorten_path --------------------------------------------------------


def test_shorten_path_deeper_than_threshold_elides_the_prefix():
    assert shorten_path("/var/a/b/c") == "…/b/c"


def test_shorten_path_exactly_at_threshold_is_unchanged():
    assert shorten_path("/b/c") == "/b/c"


def test_shorten_path_shorter_than_threshold_is_unchanged():
    assert shorten_path("/c") == "/c"


def test_shorten_path_root_is_unchanged():
    assert shorten_path("/") == "/"


def test_shorten_path_home_relative_preserves_tilde_prefix():
    assert shorten_path("~/a/b/c") == "~/…/b/c"


def test_shorten_path_home_alone_is_tilde():
    assert shorten_path("~") == "~"


def test_shorten_path_none_returns_dash():
    assert shorten_path(None) == "-"


def test_shorten_path_empty_returns_dash():
    assert shorten_path("") == "-"


def test_shorten_path_non_default_keep():
    assert shorten_path("/a/b/c/d/e", keep=3) == "…/c/d/e"


def test_shorten_path_non_default_keep_one():
    assert shorten_path("/a/b/c", keep=1) == "…/c"


def test_shorten_path_home_relative_non_default_keep():
    assert shorten_path("~/a/b/c/d", keep=3) == "~/…/b/c/d"


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
    assert rows[1].split()[0] in list(_SPINNER_FRAMES)


def test_awaiting_input_status_uses_bang():
    """Awaiting input renders a bold !."""
    node = {**PARENT, "status": "awaiting_input"}
    lines = render_tree([node])
    rows = _rows(lines[0][0])
    assert rows[1].startswith("!")


def test_dead_status_uses_cross():
    """Dead renders ✗."""
    node = {**PARENT, "status": "dead"}
    lines = render_tree([node])
    rows = _rows(lines[0][0])
    assert rows[1].startswith("✗")


def test_unknown_status_uses_question_mark():
    """An unknown status renders ?."""
    node = {**PARENT, "status": "bogus"}
    lines = render_tree([node])
    rows = _rows(lines[0][0])
    assert rows[1].startswith("?")


# ---- cwd_segments driven by config ----------------------------------------


def test_cwd_segments_controls_path_shortening():
    """shorten_path is driven by cwd_segments, not a hardcoded 2."""
    node = {**PARENT, "cwd": "/a/b/c/d/e"}
    lines_default = render_tree([node])
    lines_keep3 = render_tree([node], cwd_segments=3)
    rows_default = _rows(lines_default[0][0])
    rows_keep3 = _rows(lines_keep3[0][0])
    assert rows_default[2] == "…/d/e"
    assert rows_keep3[2] == "…/c/d/e"
