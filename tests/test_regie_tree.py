"""Tests for the régie tree rendering.

Rendering is tested against plain dicts — the same shape the daemon returns.
What can actually be wrong here is the tier mark, the status color, the
indentation of children, and whether unmanaged panes appear below the tree.
"""

from __future__ import annotations

from theater.regie.tree import render_tree, selected_participant


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


def test_children_are_indented():
    lines = render_tree([{**PARENT, "children": [CHILD]}])
    assert len(lines) == 2
    parent_label = str(lines[0][0])
    child_label = str(lines[1][0])
    # The child line should have more leading whitespace than the parent
    parent_indent = len(parent_label) - len(parent_label.lstrip())
    child_indent = len(child_label) - len(child_label.lstrip())
    assert child_indent > parent_indent


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
