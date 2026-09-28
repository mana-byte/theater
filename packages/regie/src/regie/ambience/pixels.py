"""Pixel art for ambiences: two square pixels per cell, stacked with half-block glyphs.

A canvas maps ``(x, pixel_row)`` to a colour; pixel row ``2y`` is the top half of cell row ``y``.
"""

from __future__ import annotations

from regie.ambience.scene import Cell

#: The only non-ASCII glyphs a scene may draw: the upper and lower half blocks.
HALF_BLOCKS = "\u2580\u2584"
Canvas = dict[tuple[int, int], str]


def paint(
    canvas: Canvas,
    art: tuple[str, ...],
    x: int,
    top: int,
    palette: dict[str, str],
    scale: int = 1,
) -> None:
    """Lay ``art`` with its top-left at ``(x, top)``, each of its pixels ``scale`` wide and tall.

    ``.`` is see-through; any other character is a ``palette`` key.
    """
    for dy, row in enumerate(art):
        for dx, key in enumerate(row):
            if key == ".":
                continue
            for sy in range(scale):
                for sx in range(scale):
                    canvas[(x + dx * scale + sx, top + dy * scale + sy)] = palette[key]


def to_cells(canvas: Canvas) -> list[Cell]:
    """One cell per pair of stacked pixels; an empty half stays the band's background."""
    cells = []
    for x, y in {(x, py // 2) for x, py in canvas}:
        top, bottom = canvas.get((x, 2 * y)), canvas.get((x, 2 * y + 1))
        if top and bottom:
            cells.append(Cell(x, y, HALF_BLOCKS[0], f"{top} on {bottom}"))
        elif top or bottom:
            cells.append(Cell(x, y, HALF_BLOCKS[0] if top else HALF_BLOCKS[1], top or bottom or ""))
    return sorted(cells, key=lambda cell: (cell.y, cell.x))


__all__ = ["HALF_BLOCKS", "Canvas", "paint", "to_cells"]
