"""Render the lineage tree as a Rich Text tree.

Takes the daemon's `participants.tree` output (a nested dict structure) and
`participants.unmanaged` output (flat list of panes running harnesses Theater
doesn't know about yet), and produces a `rich.text.Text` tree that the
Textual app can render in a Tree widget.

The tree has two edge kinds (spec §5):
  - Theater-spawned children: solid lines, clickable, killable
  - Harness-internal children: dashed lines, not clickable, not killable

For now, harness-internal children are not surfaced from the daemon (phase 4
will add `native_children` to the tree), so this module only renders
Theater-spawned lineage. The rendering function is written to accept both
kinds when they arrive.
"""

from __future__ import annotations

from rich.text import Text

_TIER_MARK = {"spawned": "S", "adopted": "A", "external": "E"}
_STATUS_COLOR = {
    "starting": "yellow",
    "idle": "green",
    "working": "cyan",
    "awaiting_input": "magenta",
    "dead": "red",
}


def _short_id(pid: str) -> str:
    """First 8 chars is enough to tell participants apart in a tree."""
    return pid[:8]


def _tilde(path: str | None) -> str:
    if not path:
        return "-"
    home = ""
    try:
        import os

        home = os.path.expanduser("~")
    except Exception:
        pass
    if home and path.startswith(home):
        return "~" + path[len(home):]
    return path


def _node_label(node: dict, indent: int) -> Text:
    """One line of the tree: mark tier/harness/status/cwd."""
    mark = _TIER_MARK.get(node.get("tier", ""), "?")
    reach = " " if node.get("addressable") else "*"
    harness = (node.get("harness") or "-")[:11]
    status = node.get("status", "?")
    cwd = _tilde(node.get("cwd"))
    pid = _short_id(node.get("id", "????????"))

    label = Text()
    label.append(f"{'  ' * indent}")
    label.append(f"{mark}{reach} ", style="bold")
    label.append(f"{harness:<11} ")
    label.append(f"{status:<14} ", style=_STATUS_COLOR.get(status, "white"))
    label.append(f"{pid} ")
    label.append(cwd, style="dim")
    return label


def _tree_lines(nodes: list[dict], indent: int = 0) -> list[tuple[Text, dict]]:
    """Flatten the tree into (label, node) pairs for easy rendering."""
    out: list[tuple[Text, dict]] = []
    for node in nodes:
        out.append((_node_label(node, indent), node))
        out += _tree_lines(node.get("children", []), indent + 1)
    return out


def render_tree(
    tree: list[dict], unmanaged: list[dict] | None = None
) -> list[tuple[Text, dict]]:
    """Produce (label, data) pairs for the Tree widget.

    Each participant node is a dict with id, harness, tier, status, cwd,
    tmux_pane, parent_id, addressable, and children. Unmanaged panes are
    dicts with pane, command, harness, cwd, session, window_name — they have
    no id and no children, so they are rendered as leaf nodes with a '?'
    tier mark.

    Returns a flat list so the Textual Tree can map selection back to the
    data without walking the widget's own tree.
    """
    lines = _tree_lines(tree)
    if unmanaged:
        # Separator line
        lines.append((Text("── unmanaged ──", style="dim italic"), {}))
        for u in unmanaged:
            fake_node = {
                "id": u.get("pane", "????????"),
                "tier": "external",
                "harness": u.get("harness", u.get("command", "?")),
                "status": "idle",
                "cwd": u.get("cwd"),
                "tmux_pane": u.get("pane"),
                "addressable": False,
                "children": [],
            }
            lines.append((_node_label(fake_node, 0), fake_node))
    return lines


def selected_participant(
    lines: list[tuple[Text, dict]], index: int
) -> dict | None:
    """The participant dict at a given line index, or None if it's a separator."""
    if 0 <= index < len(lines):
        node = lines[index][1]
        if node and node.get("id"):
            return node
    return None
