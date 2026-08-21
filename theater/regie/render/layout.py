"""Layout: key types, path shortening, forest walk, and the flat row list.

Takes the daemon's ``participants.tree`` output (a nested dict structure) and
``participants.unmanaged`` output (flat list of panes running harnesses Theater
doesn't know about yet), and produces a list of
``(Content, node, Key, prefix, cont_prefix)`` 5-tuples that the Textual app
renders as three-row leaves.

The leaf is three rows of Content in one widget (spec §v1.9):

    row 1: <incoming rail>, dim — blank for a root
    row 2: <rails><status glyph> <harness name> <name or short id>
    row 3: <continuation rails><shortened cwd>, dim

Row 2 carries the branch prefix (``├── `` / ``└── ``); row 3 carries the
continuation prefix (the rail or gap that follows the branch), so the tree
structure reads correctly across all three lines without a second branch
glyph appearing to start a new node. Row 1 carries the rail that leads down
into the branch, so the line does not break in the gap between siblings.
Content is used rather than Rich Text so that ``$primary`` and friends are
resolved natively by Textual against the active theme.
"""

from __future__ import annotations

# ruff: noqa: I001
from textual.content import Content

from theater.constants.regie import (
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_GAP as GAP,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_RAIL as RAIL,
)
from theater.regie.render.glyphs import node_label

#: A stable row identity for widget reconciliation; the first element namespaces the row kind.
type Key = tuple[str, str]


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

    # Separate a leading ``~`` (or ``~/``) prefix so the home mark is carried without being counted.
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


def is_root_prefix(prefix: str) -> bool:
    """Whether *prefix* is a bare branch, i.e. a root branching off the super-root."""
    return prefix in (BRANCH, LAST_BRANCH)


def _walk(
    nodes: list[dict], prefix: str = "", depth: int = 0, *, is_first_root: bool = False
) -> list[tuple[str, dict, Key, str, bool]]:
    """Depth-first walk that pairs each node with its drawn ancestry.

    Roots are drawn as siblings under an invisible super-root: they get a
    branch (``├── `` / ``└── ``) like any other child, so the whole forest
    is visually connected by rails. The super-root itself is never rendered
    — it exists only to give roots a parent to branch off. A root's prefix
    is a bare branch (no ancestry to its left), so the app can detect roots
    by checking whether the prefix is exactly ``BRANCH`` or ``LAST_BRANCH``.

    The very first root gets a blank row 1 (no rail above it): there is
    nothing visible to connect it to, and a rail hanging off the top of the
    panel reads as a missing row. Later roots keep the rail because the
    virtual parent connects them to the root above.

    Each row is ``(prefix, node, key, cont_prefix, is_first_root)`` where
    *prefix* is the branch rail for row 2 and *cont_prefix* is the
    continuation rail for row 3 (the rail or gap that follows the branch
    at this depth). *is_first_root* is consumed by :func:`node_label` to
    blank row 1 for the first root; it is not part of the app-facing
    5-tuple.
    """
    rows: list[tuple[str, dict, Key, str, bool]] = []
    last_index = len(nodes) - 1
    for i, node in enumerate(nodes):
        last = i == last_index
        if depth == 0:
            # Roots branch off the invisible super-root.
            branch = LAST_BRANCH if last else BRANCH
            child_prefix = GAP if last else RAIL
            first_root = is_first_root and i == 0
        else:
            branch = LAST_BRANCH if last else BRANCH
            child_prefix = prefix + (GAP if last else RAIL)
            first_root = False
        # cont_prefix for row 3 is the rail/gap children inherit — already child_prefix.
        cont_prefix = child_prefix
        key: Key = ("p", node.get("id", ""))
        rows.append((prefix + branch, node, key, cont_prefix, first_root))
        rows += _walk(node.get("children") or [], child_prefix, depth + 1)
    return rows


def _labelled(
    row: tuple[str, dict, Key, str, bool], *, cwd_segments: int = 2, frame: int = 0
) -> tuple[Content, dict, Key, str, str]:
    prefix, node, key, cont_prefix, is_first_root = row
    return (
        node_label(
            node,
            prefix,
            cont_prefix=cont_prefix,
            cwd_segments=cwd_segments,
            frame=frame,
            is_first_root=is_first_root,
        ),
        node,
        key,
        prefix,
        cont_prefix,
    )


def render_tree(
    tree: list[dict],
    unmanaged: list[dict] | None = None,
    *,
    cwd_segments: int = 2,
) -> list[tuple[Content, dict, Key, str, str]]:
    """Produce (label, data, key, prefix, cont_prefix) 5-tuples for the Tree widget.

    Each participant node is a dict with id, harness, tier, status, cwd,
    tmux_pane, parent_id, addressable, and children. Unmanaged panes are
    dicts with pane, command, harness, cwd, session, window_name — they have
    no id and no children, so they are rendered as leaf nodes with a ``?``
    glyph.

    The third element is a stable key the panel reconciles on: ``("p", id)``
    for participants, ``("u", pane)`` for unmanaged panes, and
    ``("sep", "unmanaged")`` for the separator. Existing ``[0]`` (label) and
    ``[1]`` (node) indexing is unaffected. The fourth element is the rail
    prefix — a bare branch (``├── `` / ``└── ``) for roots, ``""`` for the
    separator and unmanaged panes — carried explicitly so the panel can pass
    it to ``AgentLeaf`` for re-rendering on spinner ticks without re-walking
    the tree. The fifth element is the continuation prefix used for row 3
    (the cwd row), which is the rail or gap that follows the branch rather
    than a repeat of the branch itself.

    *cwd_segments* is forwarded to :func:`shorten_path` and defaults to the
    ``[regie] cwd_segments`` value. It is read from config so the tree does
    not hardcode how many directory segments to keep.

    Returns a flat list so the Textual panel can map selection back to the
    data without walking the widget's own tree.
    """
    lines = [_labelled(row, cwd_segments=cwd_segments) for row in _walk(tree, is_first_root=True)]
    if unmanaged:
        lines.append(
            (
                Content.assemble(("── unmanaged ──", "$text dim italic")),
                {},
                ("sep", "unmanaged"),
                "",
                "",
            )
        )
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
            lines.append((node_label(fake_node, cwd_segments=cwd_segments), fake_node, key, "", ""))
    return lines


def selected_participant(
    lines: list[tuple[Content, dict, Key, str, str]], index: int
) -> dict | None:
    """The participant dict at a given line index, or None if it's a separator."""
    if 0 <= index < len(lines):
        node = lines[index][1]
        if node and node.get("id"):
            return node
    return None
