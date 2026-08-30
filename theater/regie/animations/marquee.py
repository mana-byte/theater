"""Pure, cell-width-bounded text windows for hovered participant descriptions."""

from __future__ import annotations

from rich.cells import cell_len


def clip_cells(text: str, width: int) -> str:
    """Return the leading whole characters that fit in *width* terminal cells."""
    remaining = max(0, width)
    pieces: list[str] = []
    for char in text:
        size = cell_len(char)
        if size > remaining:
            break
        pieces.append(char)
        remaining -= size
    return "".join(pieces)


def overflows_cells(text: str, width: int) -> bool:
    """Whether *text* requires more terminal cells than *width* provides."""
    return cell_len(text) > max(0, width)


def marquee_cells(text: str, width: int, offset: int, *, gap: int = 3) -> str:
    """Return one right-to-left scrolling window, never wider than *width* cells."""
    if width <= 0:
        return ""
    if not overflows_cells(text, width):
        return clip_cells(text, width)

    track = text + " " * max(1, gap)
    track_width = cell_len(track)
    start = max(0, offset) % track_width
    index = 0
    consumed = 0
    while consumed < start:
        size = cell_len(track[index])
        consumed += size
        index = (index + 1) % len(track)

    remaining = width
    pieces: list[str] = []
    while remaining:
        char = track[index]
        size = cell_len(char)
        if size > remaining:
            break
        pieces.append(char)
        remaining -= size
        index = (index + 1) % len(track)
    return "".join(pieces)
