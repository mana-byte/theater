"""Plain-dict tree nodes and label inspectors shared by the tree rendering tests."""

from __future__ import annotations

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


def label_rows(label) -> list[str]:
    """Split a Content label into its three rows.

    Row 0 is the spacing row, blank for a root and the incoming rail for a
    child; row 1 is the branch row; row 2 is the cwd row.
    """
    return str(label).split("\n")


def label_styles(label) -> list[str]:
    """Styles carried by a Content label's spans."""
    return [span.style for span in label.spans]
