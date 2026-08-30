"""Pure, cell-width-bounded text windows for hovered participant descriptions."""

from __future__ import annotations

from rich.cells import cell_len

from theater.constants.regie import (
    REGIE_LEAF_MARQUEE_GAP_CELLS,
    REGIE_LEAF_MARQUEE_PAUSE_FRAMES,
)


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


def marquee_cells(
    text: str,
    width: int,
    frame: int,
    *,
    pause_frames: int = REGIE_LEAF_MARQUEE_PAUSE_FRAMES,
) -> str:
    """Return one paused, continuous left-moving window bounded to *width* cells."""
    if width <= 0:
        return ""
    if not overflows_cells(text, width):
        return clip_cells(text, width)

    track = text + " " * REGIE_LEAF_MARQUEE_GAP_CELLS
    pause = max(1, pause_frames)
    cycle_frames = pause + len(track) - 1
    phase = max(0, frame) % cycle_frames
    index = 0 if phase < pause else phase - pause + 1

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
