"""Render the lineage tree as Textual Content.

Takes the daemon's `participants.tree` output (a nested dict structure) and
`participants.unmanaged` output (flat list of panes running harnesses Theater
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

from collections import deque
from collections.abc import Mapping
from typing import NamedTuple

from textual.content import Content

from theater.formatting import short_id, tilde
from theater.harness import harness_icon

#: A stable row identity for widget reconciliation. The first element
#: namespaces the row kind so a pane id and a participant id never collide.
type Key = tuple[str, str]

#: A cell of the rail grid: ``(row, column)``, where *row* counts rendered
#: rows across the whole tree — leaf *i* owns rows ``3i``, ``3i+1``, ``3i+2``.
type Cell = tuple[int, int]

#: A cell within one three-row leaf, used for local overlays.
type LeafCell = tuple[int, int]

#: One step from a cell to the next, as ``(row delta, column delta)``.
type Direction = tuple[int, int]

UP: Direction = (-1, 0)
DOWN: Direction = (1, 0)
LEFT: Direction = (0, -1)
RIGHT: Direction = (0, 1)

#: An overlay glyph may use the default send style, or carry its own style.
type OverlayGlyph = str | tuple[str, str]

#: Rows drawn per participant leaf. The rail grid indexes in these, so the
#: constant lives beside the renderer that produces them rather than in the app.
LEAF_ROWS = 3

#: A bright style for the heavy line glyph under a send travelling the rails.
SEND_STYLE = "$accent bold"

#: Braille spinner frames, matching vibe exactly. U+28xx is unambiguously
#: narrow in every terminal, unlike the harness icons.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Ten grayscale frames so the working harness pulse stays in lockstep with
#: the ten-frame spinner: bright, dimmer, then back up without a jump.
_WORKING_HARNESS_STYLES = (
    "#FFFFFF",
    "#EFEFEF",
    "#DFDFDF",
    "#CFCFCF",
    "#BFBFBF",
    "#AFAFAF",
    "#BFBFBF",
    "#CFCFCF",
    "#DFDFDF",
    "#EFEFEF",
)

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


def is_root_prefix(prefix: str) -> bool:
    """Whether *prefix* is a bare branch, i.e. a root branching off the super-root."""
    return prefix in (_BRANCH, _LAST_BRANCH)


def _walk(
    nodes: list[dict], prefix: str = "", depth: int = 0, *, is_first_root: bool = False
) -> list[tuple[str, dict, Key, str, bool]]:
    """Depth-first walk that pairs each node with its drawn ancestry.

    Roots are drawn as siblings under an invisible super-root: they get a
    branch (``├── `` / ``└── ``) like any other child, so the whole forest
    is visually connected by rails. The super-root itself is never rendered
    — it exists only to give roots a parent to branch off. A root's prefix
    is a bare branch (no ancestry to its left), so the app can detect roots
    by checking whether the prefix is exactly ``_BRANCH`` or ``_LAST_BRANCH``.

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
            branch = _LAST_BRANCH if last else _BRANCH
            child_prefix = _GAP if last else _RAIL
            first_root = is_first_root and i == 0
        else:
            branch = _LAST_BRANCH if last else _BRANCH
            child_prefix = prefix + (_GAP if last else _RAIL)
            first_root = False
        # cont_prefix for row 3 is the same rail/gap children at this depth
        # inherit — already computed as child_prefix.
        cont_prefix = child_prefix
        key: Key = ("p", node.get("id", ""))
        rows.append((prefix + branch, node, key, cont_prefix, first_root))
        rows += _walk(node.get("children") or [], child_prefix, depth + 1)
    return rows


def spinner_frame(frame: int) -> str:
    """The braille character at *frame*, wrapping at 10."""
    return _SPINNER_FRAMES[frame % len(_SPINNER_FRAMES)]


def working_harness_style(frame: int, offset: int = 0) -> str:
    """The grayscale style for a working harness character at *offset*."""
    return _WORKING_HARNESS_STYLES[(offset - frame) % len(_WORKING_HARNESS_STYLES)]


def _append_working_harness_text(
    parts: list,
    text: str,
    *,
    frame: int,
    offset: int,
) -> None:
    """Append *text* one styled character at a time."""
    for char in text:
        if char.isspace():
            parts.append(char)
            continue
        parts.append((char, working_harness_style(frame, offset)))
        offset += 1


def _append_working_harness_parts(
    parts: list,
    harness: str,
    sid: str,
    *,
    frame: int,
    id_style: str = "",
) -> None:
    """Append the working harness as a pulse, and the name normally."""
    parts.append(" ")
    _append_working_harness_text(parts, harness, frame=frame, offset=0)
    parts.append("  ")
    if id_style:
        parts.append((sid, id_style))
    else:
        parts.append(sid)


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

    Roots now branch off an invisible super-root, so they have branch
    prefixes and this function computes a rail for them. The first root's
    rail is suppressed in :func:`node_label` via the *is_first_root* flag,
    because nothing visible sits above it and a dangling rail reads as a
    missing row.

    The ancestry to the left is copied through unchanged, gaps and all; only
    this node's own branch column is replaced. Every rail piece is the same
    width, so swapping one for another keeps the columns aligned.
    """
    if not prefix.endswith((_BRANCH, _LAST_BRANCH)):
        return ""
    return prefix[: -len(_BRANCH)] + _RAIL


def _overlay_piece(glyph: OverlayGlyph) -> tuple[str, str]:
    """Return the glyph and style for one overlay cell."""
    if isinstance(glyph, tuple):
        return glyph
    return glyph, SEND_STYLE


def _overlay_row(parts: list, overlay: Mapping[int, OverlayGlyph]) -> list:
    """Replace single characters of an assembled row by column.

    *parts* is a ``Content.assemble`` argument list — plain strings and
    ``(text, style)`` pairs — and *overlay* maps a column to the one
    character that should be drawn there instead. The part carrying the
    column is split around it so neighbouring text keeps its own style. A
    column past the end of a row is padded to: the trace sometimes crosses a
    spacer cell, and a packet that disappears there reads as a skip.
    """
    if not overlay:
        return parts
    out: list = []
    col = 0
    for part in parts:
        text = part if isinstance(part, str) else part[0]
        style = "" if isinstance(part, str) else part[1]
        start, col = col, col + len(text)
        hits = sorted(c for c in overlay if start <= c < col)
        if not hits:
            out.append(part)
            continue
        cursor = start
        for hit in hits:
            if hit > cursor:
                chunk = text[cursor - start : hit - start]
                out.append((chunk, style) if style else chunk)
            out.append(_overlay_piece(overlay[hit]))
            cursor = hit + 1
        if cursor < col:
            chunk = text[cursor - start :]
            out.append((chunk, style) if style else chunk)
    for hit in sorted(c for c in overlay if c >= col):
        if hit > col:
            out.append(" " * (hit - col))
        out.append(_overlay_piece(overlay[hit]))
        col = hit + 1
    return out


def node_label(
    node: dict,
    prefix: str = "",
    *,
    cont_prefix: str = "",
    cwd_segments: int = 2,
    frame: int = 0,
    is_first_root: bool = False,
    overlay: Mapping[LeafCell, OverlayGlyph] | None = None,
) -> Content:
    """Three rows of Content for one participant leaf.

    *overlay* maps ``(row_within_the_leaf, column)`` cells to the heavy line
    glyph drawn there — the send animation's travelling trace. It defaults to
    None, so every existing call site renders exactly as before.

    Row 1 is the spacing row — leading rather than trailing, so the first
    leaf gets breathing room under the panel border for free, and the row
    cannot be landed on by a cursor or miscounted by a test. For a child it
    is not empty: it carries the rail arriving from the parent (see
    :func:`_rail_above`), because a blank row there would break the vertical
    line in the gap between every pair of siblings. The first root's row 1
    is also blank: it branches off an invisible super-root, but nothing
    visible sits above it, so the rail is suppressed to avoid a dangling
    line at the top of the panel. Later roots keep the rail because the
    virtual parent connects them to the root above.

    Row 2 carries the *branch* prefix (``├── `` / ``└── ``); row 3 carries
    the *continuation* prefix (``cont_prefix``), which is the rail or gap
    that follows the branch at this depth. Using the branch prefix on row 3
    would make it look like a second node starts there.

    ``Content.assemble`` is used rather than line-by-line ``append`` because
    ``Content.append`` returns a new object rather than mutating in place.
    """
    glyph, glyph_style = _status_glyph(node, frame)
    # Unmanaged panes stuff a tmux pane id into "id" and have no name,
    # so the slot falls back to the short id rather than showing nothing.
    sid = node.get("name") or short_id(node.get("id"))
    id_style = _id_style(node)
    cwd = shorten_path(tilde(node.get("cwd")), keep=cwd_segments)
    harness = node.get("harness", "?")
    harness_pulse = node.get("status") == "working"

    # Row 1: the rail leading down into this node's branch. Suppressed for
    # the first root — the invisible super-root has nothing above it.
    row1_parts: list = []
    if not is_first_root:
        lead = _rail_above(prefix)
        if lead:
            row1_parts.append((lead, "$text dim"))

    # Row 2: rails, glyph, harness name, short id. The id is split out so
    # the dim-italic reach mark applies to the id portion only.
    row2_parts: list = []
    if prefix:
        row2_parts.append((prefix, "$text dim"))
    row2_parts.append((glyph, glyph_style))
    if harness_pulse:
        _append_working_harness_parts(row2_parts, harness, sid, frame=frame, id_style=id_style)
    elif id_style:
        row2_parts.append(f" {harness}  ")
        row2_parts.append((sid, id_style))
    else:
        row2_parts.append(f" {harness}  ")
        row2_parts.append(sid)

    # Row 3: continuation rails (not the branch prefix), shortened cwd, dim.
    row3_parts: list = []
    if cont_prefix:
        row3_parts.append((cont_prefix, "$text dim"))
    row3_parts.append((cwd, "$text dim"))

    if overlay:
        row1_parts = _overlay_row(row1_parts, {c: g for (r, c), g in overlay.items() if r == 0})
        row2_parts = _overlay_row(row2_parts, {c: g for (r, c), g in overlay.items() if r == 1})
        row3_parts = _overlay_row(row3_parts, {c: g for (r, c), g in overlay.items() if r == 2})

    return Content.assemble(
        *row1_parts,
        "\n",
        *row2_parts,
        "\n",
        *row3_parts,
    )


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


# ---- the rail grid --------------------------------------------------------
#
# A send is drawn as a heavy line glyph travelling the rails the tree already draws, so
# the route has to be the *visible* one — down a rail, along a branch, never
# diagonally across empty space. The rails are already fully described by the
# prefixes `_walk` computed, so the grid is derived from those strings rather
# than from anything on screen: every rail piece is four columns wide, so
# depth *d* owns columns ``4d..4d+3`` and its vertical line sits at ``4d``.


def _rail_leaves(
    lines: list[tuple[Content, dict, Key, str, str]],
) -> list[tuple[int, str, str, str, int]]:
    """The leading run of participant rows, with their depth.

    Stops at the first row that is not a participant with a branch prefix.
    The separator and the unmanaged panes below it have no rails and are one
    row tall rather than three, so they are not part of the grid — and since
    :func:`render_tree` appends them after the whole walk, stopping at the
    first one leaves exactly the rows the grid can describe.
    """
    out: list[tuple[int, str, str, str, int]] = []
    for i, (_, node, key, prefix, cont_prefix) in enumerate(lines):
        if key[0] != "p" or not prefix.endswith((_BRANCH, _LAST_BRANCH)):
            break
        out.append((i, str(node.get("id", "")), prefix, cont_prefix, len(prefix) // 4 - 1))
    return out


def _rail_cells(leaves: list[tuple[int, str, str, str, int]]) -> set[Cell]:
    """Every cell a send trace may stand on, in whole-tree row coordinates.

    Per leaf: the ancestry rails (a ``│`` in the prefix) on rows 1 and 2, the
    rail arriving into its own branch on row 1, its whole branch run from the
    branch glyph across ``── `` to the status glyph on row 2, and the
    continuation rails on row 3.

    One cell has to be added that no prefix mentions. A parent's row 3 is
    exactly as wide as its own depth, so the column its children's rail
    occupies falls past the end of it — the line is interrupted there by the
    cwd text. That gap is bridged, and a heavy line glyph is drawn there for
    the one frame the trace is passing, because the alternative is a trace
    that jumps a row.
    """
    cells: set[Cell] = set()
    prev: tuple[int, int] | None = None
    for i, _pid, prefix, cont_prefix, depth in leaves:
        top, mid, bot = LEAF_ROWS * i, LEAF_ROWS * i + 1, LEAF_ROWS * i + 2
        own = 4 * depth
        for col, char in enumerate(prefix[:own]):
            if char == _RAIL[0]:
                cells.add((mid, col))
                if i:
                    cells.add((top, col))
        # The first leaf's row 1 is blank — nothing visible sits above it.
        if i:
            cells.add((top, own))
        cells.update((mid, col) for col in range(own, own + 5))
        for col, char in enumerate(cont_prefix):
            if char == _RAIL[0]:
                cells.add((bot, col))
        if prev is not None and prev[0] == depth - 1:
            cells.add((prev[1], own))
        prev = (depth, bot)
    return cells


def _route(cells: set[Cell], start: Cell, goal: Cell) -> list[Cell] | None:
    """A shortest 4-connected route through *cells*, or None if unreachable.

    Breadth-first rather than anything cleverer: the rails are one cell wide
    and barely branch, so the grid is tiny and the shortest route through it
    *is* the route up through the common ancestor.
    """
    if start not in cells or goal not in cells:
        return None
    came: dict[Cell, Cell | None] = {start: None}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        if cur == goal:
            path: list[Cell] = []
            step: Cell | None = cur
            while step is not None:
                path.append(step)
                step = came[step]
            return list(reversed(path))
        row, col = cur
        for nxt in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
            if nxt in cells and nxt not in came:
                came[nxt] = cur
                queue.append(nxt)
    return None


def send_path(
    lines: list[tuple[Content, dict, Key, str, str]], from_id: str | None, to_id: str | None
) -> list[Cell] | None:
    """The route a send takes across the drawn tree, or None if it has none.

    Both ends must be participants currently on screen: a send from the CLI,
    from an external agent, or from a row that has since left the tree has
    nowhere to start, and returning None is how the caller drops it. The
    route runs anchor to anchor, where a leaf's anchor is its status glyph —
    the first cell after its branch.
    """
    if not from_id or not to_id or from_id == to_id:
        return None
    leaves = _rail_leaves(lines)
    anchors = {pid: (LEAF_ROWS * i + 1, 4 * (depth + 1)) for i, pid, _, _, depth in leaves if pid}
    start = anchors.get(from_id)
    goal = anchors.get(to_id)
    if start is None or goal is None:
        return None
    return _route(_rail_cells(leaves), start, goal)


def await_path(
    lines: list[tuple[Content, dict, Key, str, str]], from_id: str | None, to_id: str | None
) -> list[Cell] | None:
    """The route an await highlight takes, anchored on branch rails.

    Sends travel status-glyph to status-glyph because the packet stands for a
    prompt entering an agent. Awaits are a relationship between two leaves, so
    they begin and end on the leaves' own branch glyphs. That keeps the pulse
    on normal tree lines instead of touching status glyphs.
    """
    if not from_id or not to_id or from_id == to_id:
        return None
    leaves = _rail_leaves(lines)
    anchors = {pid: (LEAF_ROWS * i + 1, 4 * depth) for i, pid, _, _, depth in leaves if pid}
    start = anchors.get(from_id)
    goal = anchors.get(to_id)
    if start is None or goal is None:
        return None
    return _route(_rail_cells(leaves), start, goal)


def tree_glyph_at(lines: list[tuple[Content, dict, Key, str, str]], cell: Cell) -> str | None:
    """The normal tree glyph already drawn at *cell*, or None for non-rail cells.

    Route pathfinding includes a few invisible stepping-stone cells: branch
    spacer columns, the status-glyph anchors, and a gap where a child rail
    resumes below a parent's cwd row. Those are useful for a travelling send
    packet, but a persistent await highlight should tint only the rails the
    tree already draws.
    """
    leaf_index, row_in_leaf = cell_leaf(cell)
    if not 0 <= leaf_index < len(lines):
        return None
    _, _node, key, prefix, cont_prefix = lines[leaf_index]
    if key[0] != "p":
        return None
    col = cell[1]
    if col < 0:
        return None
    if row_in_leaf == 0:
        if leaf_index == 0 and is_root_prefix(prefix):
            return None
        text = _rail_above(prefix)
    elif row_in_leaf == 1:
        text = prefix
    else:
        text = cont_prefix
    if col >= len(text):
        return None
    glyph = text[col]
    return glyph if glyph in "│├└─" else None


class AwaitCell(NamedTuple):
    """One visible rail cell an await highlight passes through.

    *glyph* is the light glyph the tree already draws there, and *directions*
    the steps the route actually takes at that cell. The two together — not
    the glyph alone — decide how much of the glyph may go heavy: a route
    passing vertically through a ``├`` uses two of its three arms, and
    lighting the third would draw the passed-by sibling into a wait it has no
    part in.

    Every arm the route uses is lit, on every row, including the horizontal
    run of a branch the route only crosses. A leaf's ``── `` is the sole path
    from its own corner to the column its children's rail hangs in, so
    suppressing it breaks the line in two and the pulse appears to start in
    mid-air below the caller. Continuity is worth more than reserving the
    ``━━`` shape for the awaited leaf alone.

    *offset* is the cell's index along the route, so the grey pulse advances
    one screen column per step. Enumerating the visible cells instead would
    make the pulse jump: the route crosses invisible spacers, and two cells
    three columns apart would then pulse as if they were neighbours.
    """

    cell: Cell
    glyph: str
    directions: frozenset[Direction]
    offset: int


def _await_route(path: list[Cell]) -> list[Cell]:
    """*path*, extended along both leaves' own ``── `` toward their names.

    The route runs branch glyph to branch glyph, because that is where the
    rails end — but an await is a statement about two leaves, so the line has
    to reach both of them rather than stop one glyph short of each. The
    trailing space of ``── `` is included for direction only: it is not a
    rail, so :func:`tree_glyph_at` drops it, and its presence keeps the
    outermost drawn dash pointing at the leaf instead of tapering.

    Both ends are extended, so ``a`` awaiting ``b`` and ``b`` awaiting ``a``
    draw one picture rather than two. An edge is the same edge whichever side
    asked for it, and who waits on whom is told by the bus line; reserving
    the dashes for the awaited end instead drew a descendant's own corner as
    a bare ``┖`` where an ancestor's was a full ``┕━━``, which reads as two
    different relationships.

    An end is left alone when the route already runs along its branch: those
    cells are on the path already, with the directions to prove it.
    """
    if len(path) < 2:
        return path
    # Only the dashes and the space after them: each end's branch glyph is
    # already the first or last cell of the path.
    steps = range(1, len(_BRANCH))
    row, col = path[0]
    if path[1] != (row, col + 1):
        path = [*((row, col + step) for step in reversed(steps)), *path]
    row, col = path[-1]
    if path[-2] != (row, col + 1):
        path = [*path, *((row, col + step) for step in steps)]
    return path


def await_highlight_cells(
    lines: list[tuple[Content, dict, Key, str, str]], from_id: str | None, to_id: str | None
) -> list[AwaitCell] | None:
    """Visible tree cells to tint for an await route, with how it crosses them.

    The pathfinder may cross invisible spacer cells to keep the route
    contiguous, but the visual must only tint tree glyphs that are actually on
    that route. In particular, do not tint neighbouring ancestry rails just
    because they share a rendered row with the branch: those rails can belong
    to a sibling or to the virtual super-root rather than to the awaited child.

    Direction is carried per cell rather than reduced away, because a cell is
    not a decision: the same ``├`` is a straight-through rail for one await
    and a corner into a leaf for another, and only the caller knows which
    heavy glyph that makes.
    """
    path = await_path(lines, from_id, to_id)
    if path is None:
        return None

    route = _await_route(path)
    cells: list[AwaitCell] = []
    for index, cell in enumerate(route):
        glyph = tree_glyph_at(lines, cell)
        if glyph is None:
            continue
        row, col = cell
        directions = {
            (route[step][0] - row, route[step][1] - col)
            for step in (index - 1, index + 1)
            if 0 <= step < len(route)
        }
        cells.append(AwaitCell(cell, glyph, frozenset(directions), index))
    return cells


def cell_leaf(cell: Cell) -> tuple[int, int]:
    """Split a grid row into ``(leaf index, row within that leaf)``."""
    return divmod(cell[0], LEAF_ROWS)
