"""Pure viewport calculations for the trajectory ledger."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RowOffsets:
    """Cumulative row starts and total content height."""

    starts: tuple[int, ...]
    content_height: int


@dataclass(frozen=True, slots=True)
class RenderSlice:
    """Inclusive-exclusive row range required to render a viewport."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ScrollTarget:
    """A target content position and its corresponding row."""

    row: int
    y: int
    changed: bool


def row_offsets(row_heights: Sequence[int]) -> RowOffsets:
    """Return each row's content offset and the final content height."""
    starts: list[int] = []
    offset = 0
    for height in row_heights:
        starts.append(offset)
        offset += height
    return RowOffsets(tuple(starts), offset)


def row_at_offset(starts: Sequence[int], offset: int) -> int:
    """Return the row containing a non-negative content offset."""
    return max(0, bisect_right(starts, offset) - 1)


def max_scroll_row(
    starts: Sequence[int],
    content_height: int,
    *,
    viewport_height: int,
    row_count: int,
    viewport_rows: int,
) -> int:
    """Return the final legal scroll row, including empty-table fallback."""
    if not starts:
        return max(0, row_count - viewport_rows)
    max_y = max(0, content_height - viewport_height)
    return row_at_offset(starts, max_y)


def render_slice(
    scroll_row: int,
    *,
    row_count: int,
    viewport_rows: int,
    overscan_rows: int,
) -> RenderSlice:
    """Return the overscanned row window for a viewport."""
    return RenderSlice(
        start=max(0, scroll_row - overscan_rows),
        end=min(row_count, scroll_row + viewport_rows + overscan_rows),
    )


def target_scroll_position(
    starts: Sequence[int],
    row_heights: Sequence[int],
    *,
    row: int,
    current_y: int,
    viewport_height: int,
    content_height: int,
) -> ScrollTarget:
    """Return the smallest target that brings a row fully into view."""
    top = starts[row]
    bottom = top + row_heights[row]
    target_y = current_y
    if top < current_y:
        target_y = top
    elif bottom > current_y + viewport_height:
        target_y = max(top, bottom - viewport_height)
    if target_y == current_y:
        return ScrollTarget(row=row_at_offset(starts, current_y), y=current_y, changed=False)
    target_y = min(target_y, max(0, content_height - viewport_height))
    return ScrollTarget(row=row_at_offset(starts, target_y), y=target_y, changed=True)


__all__ = [
    "RenderSlice",
    "RowOffsets",
    "ScrollTarget",
    "max_scroll_row",
    "render_slice",
    "row_at_offset",
    "row_offsets",
    "target_scroll_position",
]
