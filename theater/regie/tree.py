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

from typing import TypeAlias

from rich.text import Text

from theater.formatting import (
    clip_harness,
    reach_mark,
    short_id,
    tier_mark,
    tilde,
)
from theater.harness import harness_icon

#: A stable identity for a row, used by the panel to reconcile widgets
#: across refreshes rather than rebuilding the whole tree each tick.
#: The first element namespaces the row kind so a pane id and a
#: participant id can never collide.
Key: TypeAlias = tuple[str, str]

_STATUS_COLOR = {
    "idle": "green",
    "working": "cyan",
    "awaiting_input": "magenta",
    "dead": "red",
}

#: Box-drawing pieces for the lineage rails. Plain indentation could not say
#: whether the agent two lines down is a sibling or a nephew; the rails can.
_BRANCH = "├── "
_LAST_BRANCH = "└── "
_RAIL = "│   "
_GAP = "    "


def shorten_path(path: str | None, keep: int = 2) -> str:
    """Keep the last *keep* segments, elide the rest with ``…/``.

    Applied *after* :func:`theater.formatting.tilde`, so ``~`` is a preserved
    prefix and is not counted as a segment::

        ~/a/b/c  ->  ~/…/b/c
        /var/a/b/c -> …/b/c

    A path already at or under the threshold is returned unchanged.
    ``None`` or empty behaves like ``tilde()`` — returns ``"-"``.
    """
    if not path:
        return "-"

    # Separate a leading ``~`` (or ``~/``) prefix from the segments that
    # follow, so the home mark is carried through without being counted.
    prefix = ""
    rest = path
    if rest.startswith("~/"):
        prefix = "~/"
        rest = rest[2:]
    elif rest == "~":
        return "~"

    segments = [s for s in rest.split("/") if s]

    if len(segments) <= keep:
        return path

    tail = "/".join(segments[-keep:])
    return f"{prefix}…/{tail}"


def _walk(nodes: list[dict], prefix: str = "", depth: int = 0) -> list[tuple[str, dict, Key]]:
    """Depth-first walk that pairs each node with its drawn ancestry.

    Roots get no branch of their own: they are separate agents, not siblings
    under some invisible parent, and a rail hanging off nothing reads as a
    missing row. Children get a branch, and the rail continues past them only
    while their parent still has siblings below.
    """
    rows: list[tuple[str, dict, Key]] = []
    last_index = len(nodes) - 1
    for i, node in enumerate(nodes):
        last = i == last_index
        if depth == 0:
            branch, child_prefix = "", ""
        else:
            branch = _LAST_BRANCH if last else _BRANCH
            child_prefix = prefix + (_GAP if last else _RAIL)
        key: Key = ("p", node.get("id", ""))
        rows.append((prefix + branch, node, key))
        rows += _walk(node.get("children") or [], child_prefix, depth + 1)
    return rows


def _node_label(node: dict, prefix: str = "") -> Text:
    """One line of the tree: rails, then mark tier/harness/status/cwd."""
    status = node.get("status", "?")

    label = Text()
    if prefix:
        label.append(prefix, style="dim")
    marks = f"{tier_mark(node.get('tier'))}{reach_mark(node.get('addressable'))} "
    label.append(marks, style="bold")
    label.append(f"{harness_icon(node.get('harness'))} ")
    label.append(f"{clip_harness(node.get('harness')):<11} ")
    label.append(f"{status:<14} ", style=_STATUS_COLOR.get(status, "white"))
    label.append(f"{short_id(node.get('id'))} ")
    label.append(tilde(node.get("cwd")), style="dim")
    return label


def _labelled(row: tuple[str, dict, Key]) -> tuple[Text, dict, Key]:
    prefix, node, key = row
    return _node_label(node, prefix), node, key


def render_tree(
    tree: list[dict], unmanaged: list[dict] | None = None
) -> list[tuple[Text, dict, Key]]:
    """Produce (label, data, key) triples for the Tree widget.

    Each participant node is a dict with id, harness, tier, status, cwd,
    tmux_pane, parent_id, addressable, and children. Unmanaged panes are
    dicts with pane, command, harness, cwd, session, window_name — they have
    no id and no children, so they are rendered as leaf nodes with a '?'
    tier mark.

    The third element is a stable key the panel reconciles on: ``("p", id)``
    for participants, ``("u", pane)`` for unmanaged panes, and
    ``("sep", "unmanaged")`` for the separator. Existing ``[0]`` (label) and
    ``[1]`` (node) indexing is unaffected.

    Returns a flat list so the Textual Tree can map selection back to the
    data without walking the widget's own tree.
    """
    lines = [_labelled(row) for row in _walk(tree)]
    if unmanaged:
        # Separator line
        lines.append((Text("── unmanaged ──", style="dim italic"), {}, ("sep", "unmanaged")))
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
            key: Key = ("u", u.get("pane", ""))
            lines.append((_node_label(fake_node), fake_node, key))
    return lines


def selected_participant(
    lines: list[tuple[Text, dict, Key]], index: int
) -> dict | None:
    """The participant dict at a given line index, or None if it's a separator."""
    if 0 <= index < len(lines):
        node = lines[index][1]
        if node and node.get("id"):
            return node
    return None
