"""Render the lineage tree as Textual Content.

Takes the daemon's `participants.tree` output (a nested dict structure) and
`participants.unmanaged` output (flat list of panes running harnesses Theater
doesn't know about yet), and produces a list of ``(Content, node, Key, prefix)``
4-tuples that the Textual app renders as three-row leaves.

The leaf is three rows of Content in one widget (spec §v1.9):

    row 1: blank spacing
    row 2: <rails><status glyph> <harness name> <short id>
    row 3: <rails><shortened cwd>, dim

Rails are carried on both content rows or the tree structure breaks visually.
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


def node_label(
    node: dict, prefix: str = "", *, cwd_segments: int = 2, frame: int = 0
) -> Content:
    """Three rows of Content for one participant leaf.

    Row 1 is blank spacing — leading rather than trailing, so the first leaf
    gets breathing room under the panel border for free, and the row cannot
    be landed on by a cursor or miscounted by a test.

    Rails (``prefix``) are carried on both content rows. A tree whose rails
    work on every other line is not a tree.

    ``Content.assemble`` is used rather than line-by-line ``append`` because
    ``Content.append`` returns a new object rather than mutating in place.
    """
    glyph, glyph_style = _status_glyph(node, frame)
    sid = short_id(node.get("id"))
    id_style = _id_style(node)
    cwd = shorten_path(tilde(node.get("cwd")), keep=cwd_segments)
    harness = node.get("harness", "?")

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

    # Row 3: rails (same prefix), shortened cwd, dim.
    row3_parts: list = []
    if prefix:
        row3_parts.append((prefix, "$text dim"))
    row3_parts.append((cwd, "$text dim"))

    return Content.assemble(
        "",
        "\n",
        *row2_parts,
        "\n",
        *row3_parts,
    )


def _labelled(
    row: tuple[str, dict, Key], *, cwd_segments: int = 2, frame: int = 0
) -> tuple[Content, dict, Key, str]:
    prefix, node, key = row
    return node_label(node, prefix, cwd_segments=cwd_segments, frame=frame), node, key, prefix


def render_tree(
    tree: list[dict],
    unmanaged: list[dict] | None = None,
    *,
    cwd_segments: int = 2,
) -> list[tuple[Content, dict, Key, str]]:
    """Produce (label, data, key, prefix) 4-tuples for the Tree widget.

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
    spinner ticks without re-walking the tree.

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
                (node_label(fake_node, cwd_segments=cwd_segments), fake_node, key, "")
            )
    return lines


def selected_participant(
    lines: list[tuple[Content, dict, Key, str]], index: int
) -> dict | None:
    """The participant dict at a given line index, or None if it's a separator."""
    if 0 <= index < len(lines):
        node = lines[index][1]
        if node and node.get("id"):
            return node
    return None
