"""Render the lineage tree as Textual Content.

Takes the daemon's `participants.tree` output (a nested dict structure) and
`participants.unmanaged` output (flat list of panes running harnesses Theater
doesn't know about yet), and produces a list of
``(Content, node, Key, prefix, cont_prefix)`` 5-tuples that the Textual app
renders as three-row leaves.

The leaf is three rows of Content in one widget (spec §v1.9):

    row 1: <incoming rail>, dim — blank for a root
    row 2: <rails><status glyph> <harness name> <short id>
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

from typing import TypeAlias

from textual.content import Content

from theater.formatting import short_id, tilde
from theater.harness import harness_icon

#: A stable identity for a row, used by the panel to reconcile widgets
#: across refreshes rather than rebuilding the whole tree each tick.
#: The first element namespaces the row kind so a pane id and a
#: participant id can never collide.
Key: TypeAlias = tuple[str, str]

#: Braille spinner frames, matching vibe exactly. U+28xx is unambiguously
#: narrow in every terminal, unlike the harness icons.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

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


def _walk(
    nodes: list[dict], prefix: str = "", depth: int = 0
) -> list[tuple[str, dict, Key, str]]:
    """Depth-first walk that pairs each node with its drawn ancestry.

    Roots get no branch of their own: they are separate agents, not siblings
    under some invisible parent, and a rail hanging off nothing reads as a
    missing row. Children get a branch, and the rail continues past them only
    while their parent still has siblings below.

    Each row is ``(prefix, node, key, cont_prefix)`` where *prefix* is the
    branch rail for row 2 and *cont_prefix* is the continuation rail for row 3
    (the rail or gap that follows the branch at this depth).
    """
    rows: list[tuple[str, dict, Key, str]] = []
    last_index = len(nodes) - 1
    for i, node in enumerate(nodes):
        last = i == last_index
        if depth == 0:
            branch, child_prefix = "", ""
        else:
            branch = _LAST_BRANCH if last else _BRANCH
            child_prefix = prefix + (_GAP if last else _RAIL)
        # The continuation prefix for row 3 is the same rail/gap that
        # children at this depth would inherit — it is already computed
        # as child_prefix, including the "" case for roots.
        cont_prefix = child_prefix
        key: Key = ("p", node.get("id", ""))
        rows.append((prefix + branch, node, key, cont_prefix))
        rows += _walk(node.get("children") or [], child_prefix, depth + 1)
    return rows


def spinner_frame(frame: int) -> str:
    """The braille character at *frame*, wrapping at 10."""
    return _SPINNER_FRAMES[frame % len(_SPINNER_FRAMES)]


def _status_glyph(node: dict, frame: int = 0) -> tuple[str, str]:
    """The one-character status mark and the theme slot it renders in.

    Returns ``(glyph, style)`` where *style* is a Textual design-token string
    like ``"$primary"``. Idle uses the harness's own icon so the glyph does
    double duty; the separate harness-glyph column is gone.
    """
    status = node.get("status", "?")
    if status == "working":
        return spinner_frame(frame), "$primary"
    if status == "awaiting_input":
        return "!", "$warning"
    if status == "dead":
        return "✗", "$error"
    if status == "idle":
        return harness_icon(node.get("harness")), "$text-muted"
    # Unknown / unmanaged: honest "?" rather than guessing idle.
    return "?", "$text-muted"


def _id_style(node: dict) -> str:
    """A dim-italic id means the participant cannot be sent to.

    The old ``reach_mark`` glyph (``*``) is re-expressed as a style: same
    information, zero columns, and a reader who does not know the convention
    still gets the right impression from a greyed-out row.
    """
    return "$text dim italic" if not node.get("addressable", True) else ""


def _rail_above(prefix: str) -> str:
    """The rail for row 1: the line that leads down into this node's branch.

    Row 2 draws ``├── `` or ``└── `` at this node's own depth, and the line
    arriving there comes down from the parent — so the cell directly above
    the branch glyph is a rail. That holds for a last child too: ``└``
    closes a line that comes from above rather than starting one, so its row
    1 is a rail like everyone else's. Only row 3 turns on last-ness, because
    row 3 is where the line either continues past this node or stops.

    A root gets nothing. It has no branch, so there is no line above it to
    continue, and a rail hanging off nothing reads as a sibling that failed
    to render.

    The ancestry to the left is copied through unchanged, gaps and all; only
    this node's own branch column is replaced. Every rail piece is the same
    width, so swapping one for another keeps the columns aligned.
    """
    if not prefix.endswith((_BRANCH, _LAST_BRANCH)):
        return ""
    return prefix[: -len(_BRANCH)] + _RAIL


def node_label(
    node: dict,
    prefix: str = "",
    *,
    cont_prefix: str = "",
    cwd_segments: int = 2,
    frame: int = 0,
) -> Content:
    """Three rows of Content for one participant leaf.

    Row 1 is the spacing row — leading rather than trailing, so the first
    leaf gets breathing room under the panel border for free, and the row
    cannot be landed on by a cursor or miscounted by a test. For a child it
    is not empty: it carries the rail arriving from the parent (see
    :func:`_rail_above`), because a blank row there would break the vertical
    line in the gap between every pair of siblings. A root's row 1 stays
    blank.

    Row 2 carries the *branch* prefix (``├── `` / ``└── ``); row 3 carries
    the *continuation* prefix (``cont_prefix``), which is the rail or gap
    that follows the branch at this depth. Using the branch prefix on row 3
    would make it look like a second node starts there.

    ``Content.assemble`` is used rather than line-by-line ``append`` because
    ``Content.append`` returns a new object rather than mutating in place.
    """
    glyph, glyph_style = _status_glyph(node, frame)
    sid = short_id(node.get("id"))
    id_style = _id_style(node)
    cwd = shorten_path(tilde(node.get("cwd")), keep=cwd_segments)
    harness = node.get("harness", "?")

    # Row 1: the rail leading down into this node's branch, blank for a root.
    row1_parts: list = []
    lead = _rail_above(prefix)
    if lead:
        row1_parts.append((lead, "$text dim"))

    # Row 2: rails, glyph, harness name, short id. The id is split out so
    # the dim-italic reach mark applies to the id portion only.
    row2_parts: list = []
    if prefix:
        row2_parts.append((prefix, "$text dim"))
    row2_parts.append((glyph, glyph_style))
    row2_parts.append(f" {harness}  ")
    if id_style:
        row2_parts.append((sid, id_style))
    else:
        row2_parts.append(sid)

    # Row 3: continuation rails (not the branch prefix), shortened cwd, dim.
    row3_parts: list = []
    if cont_prefix:
        row3_parts.append((cont_prefix, "$text dim"))
    row3_parts.append((cwd, "$text dim"))

    return Content.assemble(
        *row1_parts,
        "\n",
        *row2_parts,
        "\n",
        *row3_parts,
    )


def _labelled(
    row: tuple[str, dict, Key, str], *, cwd_segments: int = 2, frame: int = 0
) -> tuple[Content, dict, Key, str, str]:
    prefix, node, key, cont_prefix = row
    return (
        node_label(node, prefix, cont_prefix=cont_prefix, cwd_segments=cwd_segments, frame=frame),
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
    prefix (``""`` for roots, the separator, and unmanaged panes), carried
    explicitly so the panel can pass it to ``AgentLeaf`` for re-rendering on
    spinner ticks without re-walking the tree. The fifth element is the
    continuation prefix used for row 3 (the cwd row), which is the rail or
    gap that follows the branch rather than a repeat of the branch itself.

    *cwd_segments* is forwarded to :func:`shorten_path` and defaults to the
    ``[regie] cwd_segments`` value. It is read from config so the tree does
    not hardcode how many directory segments to keep.

    Returns a flat list so the Textual panel can map selection back to the
    data without walking the widget's own tree.
    """
    lines = [_labelled(row, cwd_segments=cwd_segments) for row in _walk(tree)]
    if unmanaged:
        # Separator line
        lines.append((
            Content.assemble(("── unmanaged ──", "$text dim italic")),
            {},
            ("sep", "unmanaged"),
            "",
            "",
        ))
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
            lines.append(
                (node_label(fake_node, cwd_segments=cwd_segments), fake_node, key, "", "")
            )
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
