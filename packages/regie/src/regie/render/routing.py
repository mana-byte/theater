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
from regie.render.glyphs import LeafCell, OverlayGlyph, _rail_above, separator_prefix
from regie.render.layout import Key, TreeLines, is_root_prefix, row_count

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


class _RailEntry(NamedTuple):
    line_index: int
    top: int
    participant_id: str | None
    prefix: str
    cont_prefix: str
    depth: int
    height: int


def _rail_leaves(
    lines: list[tuple[Content, dict, Key, str, str]],
) -> list[_RailEntry]:
    """The managed tree rows, with absolute rendered coordinates."""
    out: list[_RailEntry] = []
    top = 0
    for line_index, (_, node, key, prefix, cont_prefix) in enumerate(lines):
        if key[0] not in {"p", "s"} or not prefix.endswith((BRANCH, LAST_BRANCH)):
            break
        height = row_count(key)
        participant_id = str(node.get("id", "")) if key[0] == "p" else None
        out.append(
            _RailEntry(
                line_index,
                top,
                participant_id,
                prefix,
                cont_prefix,
                len(prefix) // 4 - 1,
                height,
            )
        )
        top += height
    return out


def _rail_cells(entries: list[_RailEntry]) -> set[Cell]:
    """Every cell a send trace may stand on, in whole-tree row coordinates.

    Also bridges one cell no prefix mentions: the children's rail column on a parent's
    row 3, hidden by cwd text, so the trace doesn't jump a row.
    """
    cells: set[Cell] = set()
    prev: tuple[int, int] | None = None
    for entry in entries:
        own = 4 * entry.depth
        if entry.participant_id is None:
            top, mid, bot = entry.top, entry.top + 1, entry.top + 2
            # The first row's rail is blank for the first root, as on a leaf.
            if top:
                cells.update(
                    (top, col) for col, c in enumerate(_rail_above(entry.prefix)) if c == RAIL[0]
                )
            # The heading sits on the tree's own branch; its dashes are not a route.
            cells.update((mid, col) for col, c in enumerate(entry.prefix) if c in "│├└")
            rails = separator_prefix(entry.prefix)
            cells.update((bot, col) for col, c in enumerate(rails) if c == RAIL[0])
            if prev is not None and prev[0] == entry.depth - 1:
                cells.add((prev[1], own))
            prev = (entry.depth, bot)
            continue
        top, mid, bot = entry.top, entry.top + 1, entry.top + 2
        for col, char in enumerate(entry.prefix[:own]):
            if char == RAIL[0]:
                cells.add((mid, col))
                if top:
                    cells.add((top, col))
        # The first leaf's row 1 is blank — nothing visible sits above it.
        if top:
            cells.add((top, own))
        cells.update((mid, col) for col in range(own, own + 5))
        for col, char in enumerate(entry.cont_prefix):
            if char == RAIL[0]:
                cells.add((bot, col))
        if prev is not None and prev[0] == entry.depth - 1:
            cells.add((prev[1], own))
        prev = (entry.depth, bot)
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
    anchors = {
        entry.participant_id: (entry.top + 1, 4 * (entry.depth + 1))
        for entry in leaves
        if entry.participant_id
    }
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
    anchors = {
        entry.participant_id: (entry.top + 1, 4 * entry.depth)
        for entry in leaves
        if entry.participant_id
    }
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
    leaf_index, row_in_leaf = cell_leaf(cell, lines)
    if not 0 <= leaf_index < len(lines):
        return None
    _, _node, key, prefix, cont_prefix = lines[leaf_index]
    if key[0] == "s":
        rail = "" if leaf_index == 0 and is_root_prefix(prefix) else _rail_above(prefix)
        text = (rail, prefix, separator_prefix(prefix))[min(row_in_leaf, 2)]
        col = cell[1]
        glyph = text[col] if 0 <= col < len(text) else ""
        return glyph if glyph in "│├└─" else None
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


def cell_leaf(
    cell: Cell,
    lines: list[tuple[Content, dict, Key, str, str]] | None = None,
) -> tuple[int, int]:
    """Split a grid row into ``(leaf index, row within that leaf)``."""
    if not isinstance(lines, TreeLines):
        return divmod(cell[0], LEAF_ROWS)
    row = cell[0]
    return lines.row_lookup[row] if 0 <= row < len(lines.row_lookup) else (len(lines), 0)


#: Which arms each rail glyph the tree draws actually has.
_RAIL_ARMS: dict[str, frozenset[Direction]] = {
    "│": frozenset({UP, DOWN}),
    "─": frozenset({LEFT, RIGHT}),
    "└": frozenset({UP, RIGHT}),
    "├": frozenset({UP, DOWN, RIGHT}),
}

#: The heavy form of each rail glyph by which of its arms are drawn heavy.
_HEAVY_RAILS: dict[tuple[str, frozenset[Direction]], str] = {
    ("│", frozenset({UP, DOWN})): "┃",
    ("│", frozenset({UP})): "╿",
    ("│", frozenset({DOWN})): "╽",
    ("─", frozenset({LEFT, RIGHT})): "━",
    ("─", frozenset({LEFT})): "╾",
    ("─", frozenset({RIGHT})): "╼",
    ("└", frozenset({UP, RIGHT})): "┗",
    ("└", frozenset({UP})): "┖",
    ("└", frozenset({RIGHT})): "┕",
    ("├", frozenset({UP, DOWN, RIGHT})): "┣",
    ("├", frozenset({UP, DOWN})): "┠",
    ("├", frozenset({UP, RIGHT})): "┡",
    ("├", frozenset({DOWN, RIGHT})): "┢",
    ("├", frozenset({UP})): "┞",
    ("├", frozenset({DOWN})): "┟",
    ("├", frozenset({RIGHT})): "┝",
}

#: A separator's heavy rails keep the tree's own dim colour: only their weight changes, so they
#: stay quieter than an await route's pulse (never darker than #AFAFAF) or a send trace.
SECTION_RAIL_STYLE = "$text dim"

#: Heavy arms to draw, by row key and ``(row within the row, column)``.
type RailArms = dict[Key, dict[LeafCell, frozenset[Direction]]]


def heavy_rail(glyph: str, arms: frozenset[Direction]) -> str:
    """*glyph* with *arms* drawn heavy and its other arms left light; unchanged if none apply."""
    used = _RAIL_ARMS.get(glyph)
    if used is None:
        return glyph
    return _HEAVY_RAILS.get((glyph, arms & used), glyph)


def section_arms(lines: list[tuple[Content, dict, Key, str, str]], separator: Key) -> RailArms:
    """The rail arms from a separator's own branch down to every agent it can fold.

    Its section is its later siblings and their descendants, up to its next sibling separator.
    An arm is included only where it leads on to one of them; levels above it never are.
    """
    keys = [key for _, _, key, _, _ in lines]
    if separator not in keys:
        return {}
    width = len(BRANCH)
    levels = [len(prefix) // width - 1 for _, _, _, prefix, _ in lines]
    start, top = keys.index(separator), levels[keys.index(separator)]
    end = next(
        (
            row
            for row in range(start + 1, len(lines))
            if levels[row] < top or (levels[row] == top and keys[row][0] == "s")
        ),
        len(lines),
    )

    def leads_on(row: int, level: int) -> bool:
        """Whether the rail at *level* beside *row* runs on down to a sibling in the section."""
        below = next((i for i in range(row + 1, len(lines)) if levels[i] <= level), None)
        return below is not None and below < end and levels[below] == level

    both = frozenset({UP, DOWN})
    arms: RailArms = {}
    for row in range(start, end):
        level, cells = levels[row], arms.setdefault(keys[row], {})
        for passing in (g for g in range(top, level) if leads_on(row, g)):
            for line in (0, 1, 2):
                cells[(line, passing * width)] = both
        col, onward = level * width, leads_on(row, level)
        into = row != start  # the separator's branch starts the path; an agent's is reached
        if into:
            cells[(0, col)] = both
        cells[(1, col)] = frozenset({RIGHT, *([UP] if into else []), *([DOWN] if onward else [])})
        for dash in range(col + 1, col + width - 1):
            cells[(1, dash)] = frozenset({LEFT, RIGHT})
        if onward:
            cells[(2, col)] = both
    return arms


def heavy_overlays(
    lines: list[tuple[Content, dict, Key, str, str]], arms: RailArms
) -> dict[Key, dict[LeafCell, OverlayGlyph]]:
    """Per row, the heavy glyphs *arms* draw over its own light rails."""
    rows = {key: label.plain.split("\n") for label, _, key, _, _ in lines}
    overlays: dict[Key, dict[LeafCell, OverlayGlyph]] = {}
    for key, cells in arms.items():
        text = rows.get(key, [])
        for (line, col), wanted in cells.items():
            glyph = text[line][col] if line < len(text) and col < len(text[line]) else None
            heavy = heavy_rail(glyph, wanted) if glyph is not None else None
            if heavy is not None and heavy != glyph:
                overlays.setdefault(key, {})[(line, col)] = (heavy, SECTION_RAIL_STYLE)
    return overlays
