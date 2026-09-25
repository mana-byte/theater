"""Routing: the rail grid, BFS pathfinding, send/await traces, and await highlights.

Routes follow the visible rails, never diagonals. The grid comes from the walk's
prefixes: rail pieces are four columns, so depth *d*'s vertical line sits at ``4d``.
"""

from __future__ import annotations

# ruff: noqa: I001
from collections import deque
from typing import NamedTuple

from textual.content import Content

from regie.ui_constants import (
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_LEAF_ROWS as LEAF_ROWS,
    REGIE_TREE_RAIL as RAIL,
)
from regie.render.glyphs import _rail_above
from regie.render.layout import Key, is_root_prefix

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

    *directions* keep passed-by siblings out of the wait yet light every used arm;
    *offset* is the route index, not the visible one, so the pulse doesn't jump spacers.
    """

    cell: Cell
    glyph: str
    directions: frozenset[Direction]
    offset: int


def _rail_leaves(
    lines: list[tuple[Content, dict, Key, str, str]],
) -> list[tuple[int, str, str, str, int]]:
    """The leading run of participant rows, with their depth.

    Stops at the separator: unmanaged rows have no rails and are one row tall, and
    :func:`render_tree` appends them after the walk.
    """
    out: list[tuple[int, str, str, str, int]] = []
    for i, (_, node, key, prefix, cont_prefix) in enumerate(lines):
        if key[0] != "p" or not prefix.endswith((BRANCH, LAST_BRANCH)):
            break
        out.append((i, str(node.get("id", "")), prefix, cont_prefix, len(prefix) // 4 - 1))
    return out


def _rail_cells(leaves: list[tuple[int, str, str, str, int]]) -> set[Cell]:
    """Every cell a send trace may stand on, in whole-tree row coordinates.

    Also bridges one cell no prefix mentions: the children's rail column on a parent's
    row 3, hidden by cwd text, so the trace doesn't jump a row.
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

    Plain BFS suffices: the grid is tiny, and its shortest route is the one through
    the common ancestor.
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

    None drops sends whose ends aren't on screen (CLI, external, departed rows).
    Anchors are status glyphs, since the packet is a prompt entering an agent.
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

    Awaits relate two leaves, so they anchor on branch glyphs and never touch status glyphs.
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

    Filters out invisible stepping-stone cells: fine for a send packet, but a
    persistent await highlight should tint only drawn rails.
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

    Both ends extend so ``a``→``b`` and ``b``→``a`` look identical (one end drew ``┖``
    vs ``┕━━``); the bus line says who waits. The trailing space only orients the dash.
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

    Never tints same-row ancestry rails, which may belong to a sibling or super-root.
    Direction is per cell: one ``├`` is straight-through for one await, a corner for another.
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
