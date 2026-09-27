"""Cells to Textual Content: one line per band row, blanks where nothing is drawn."""

from __future__ import annotations

from collections.abc import Iterable

from rich.cells import cell_len
from textual.content import Content

from regie.ambience.scene import Cell


def render_band(cells: Iterable[Cell], width: int, height: int) -> Content:
    """Lay cells on a ``width`` × ``height`` grid; later cells win, wide glyphs take two."""
    grid: list[list[tuple[str, str] | None]] = [[None] * width for _ in range(height)]
    for cell in cells:
        span = max(1, cell_len(cell.glyph))
        if not (0 <= cell.y < height and cell.x >= 0 and cell.x + span <= width):
            continue
        row = grid[cell.y]
        row[cell.x] = (cell.glyph, cell.style)
        for extra in range(1, span):
            row[cell.x + extra] = ("", "")  # covered by the wide glyph to its left
    lines = []
    for row in grid:
        parts: list[str | tuple[str, str]] = []
        for entry in row:
            if entry is None:
                parts.append(" ")
            elif entry[0]:
                parts.append(entry if entry[1] else entry[0])
        lines.append(Content.assemble(*parts))
    return Content("\n").join(lines)


__all__ = ["render_band"]
