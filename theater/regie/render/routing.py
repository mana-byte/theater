"""Routing: the rail grid, BFS pathfinding, send/await traces, and await highlights.

A send is drawn as a heavy line glyph travelling the rails the tree already draws,
so the route has to be the *visible* one — down a rail, along a branch, never
diagonally across empty space. The rails are already fully described by the
prefixes the forest walk computed, so the grid is derived from those strings
rather than from anything on screen: every rail piece is four columns wide, so
depth *d* owns columns ``4d..4d+3`` and its vertical line sits at ``4d``.
"""

from __future__ import annotations

# ruff: noqa: I001
from collections import deque
from typing import NamedTuple

from textual.content import Content

from theater.constants.regie import (
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_LEAF_ROWS as LEAF_ROWS,
    REGIE_TREE_RAIL as RAIL,
)
from theater.regie.render.glyphs import _rail_above
from theater.regie.render.layout import Key, is_root_prefix

#: A cell of the rail grid: ``(row, column)``, *row* counts rendered rows across the tree.
type Cell = tuple[int, int]

#: One step from a cell to the next, as ``(row delta, column delta)``.
type Direction = tuple[int, int]

UP: Direction = (-1, 0)
DOWN: Direction = (1, 0)
LEFT: Direction = (0, -1)
RIGHT: Direction = (0, 1)


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
        if key[0] != "p" or not prefix.endswith((BRANCH, LAST_BRANCH)):
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
            if char == RAIL[0]:
                cells.add((mid, col))
                if i:
                    cells.add((top, col))
        # The first leaf's row 1 is blank — nothing visible sits above it.
        if i:
            cells.add((top, own))
        cells.update((mid, col) for col in range(own, own + 5))
        for col, char in enumerate(cont_prefix):
            if char == RAIL[0]:
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
    # Only dashes and space after: each end's branch glyph is already first or last cell.
    steps = range(1, len(BRANCH))
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
