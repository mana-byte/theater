"""Tests for the régie tree rendering.

Rendering is tested against plain dicts — the same shape the daemon returns.
What can actually be wrong here is the tier mark, the status color, the
indentation of children, and whether unmanaged panes appear below the tree.
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


def test_empty_tree_renders_no_participants():
    lines = render_tree([])
    assert len(lines) == 0


def test_single_participant_renders_with_tier_mark():
    lines = render_tree([{**PARENT, "children": []}])
    assert len(lines) == 1
    label = lines[0][0]
    assert "S" in str(label)  # spawned
    assert "vibe" in str(label)
    assert "working" in str(label)
    assert "aabbccdd" in str(label)  # short id


def test_children_hang_off_a_branch():
    """Indentation alone could not tell a sibling from a nephew. Rails can."""
    lines = render_tree([{**PARENT, "children": [CHILD]}])
    assert len(lines) == 2
    assert not str(lines[0][0]).startswith(("├", "└", " "))
    assert str(lines[1][0]).startswith("└── ")


def test_only_the_last_sibling_closes_the_branch():
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    assert str(lines[1][0]).startswith("├── ")
    assert str(lines[2][0]).startswith("└── ")


def test_the_rail_continues_past_a_parent_that_has_siblings_below():
    """A grandchild under a non-last child still shows its aunt's line."""
    grandchild = {**CHILD, "id": "ddeeff001122"}
    first = {**CHILD, "children": [grandchild]}
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [first, second]}])
    assert str(lines[1][0]).startswith("├── ")
    assert str(lines[2][0]).startswith("│   └── ")
    assert str(lines[3][0]).startswith("└── ")


def test_the_rail_stops_under_a_last_child():
    grandchild = {**CHILD, "id": "ddeeff001122"}
    lines = render_tree([{**PARENT, "children": [{**CHILD, "children": [grandchild]}]}])
    assert str(lines[1][0]).startswith("└── ")
    assert str(lines[2][0]).startswith("    └── ")


def test_separate_roots_are_not_drawn_as_siblings():
    """Two unrelated agents are not children of anything, so no rails."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    assert all(not str(line[0]).startswith(("├", "└")) for line in lines)


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


def test_external_tier_mark_renders():
    ext = {**PARENT, "tier": "external", "addressable": False}
    lines = render_tree([ext])
    assert "E" in str(lines[0][0])
    assert "*" in str(lines[0][0])  # not addressable


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
