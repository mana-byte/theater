"""Pure ledger viewport boundary coverage."""

from __future__ import annotations

from theater.regie.trajectory.widgets.ledger_viewport import (
    RenderSlice,
    ScrollTarget,
    max_scroll_row,
    render_slice,
    row_at_offset,
    row_offsets,
    target_scroll_position,
)


def test_row_offsets_and_maximum_scroll_cover_empty_and_variable_rows() -> None:
    offsets = row_offsets((2, 1, 3))

    assert offsets.starts == (0, 2, 3)
    assert offsets.content_height == 6
    assert row_at_offset(offsets.starts, 0) == 0
    assert row_at_offset(offsets.starts, 2) == 1
    assert row_at_offset(offsets.starts, 4) == 2
    assert (
        max_scroll_row(
            offsets.starts,
            offsets.content_height,
            viewport_height=3,
            row_count=3,
            viewport_rows=2,
        )
        == 2
    )
    assert max_scroll_row((), 0, viewport_height=3, row_count=10, viewport_rows=3) == 7


def test_render_slice_and_scroll_target_honor_viewport_boundaries() -> None:
    offsets = row_offsets((2, 1, 3))

    assert render_slice(0, row_count=10, viewport_rows=3, overscan_rows=2) == RenderSlice(0, 5)
    assert render_slice(4, row_count=10, viewport_rows=3, overscan_rows=2) == RenderSlice(2, 9)
    assert target_scroll_position(
        offsets.starts,
        (2, 1, 3),
        row=0,
        current_y=3,
        viewport_height=3,
        content_height=offsets.content_height,
    ) == ScrollTarget(row=0, y=0, changed=True)
    assert target_scroll_position(
        offsets.starts,
        (2, 1, 3),
        row=2,
        current_y=0,
        viewport_height=3,
        content_height=offsets.content_height,
    ) == ScrollTarget(row=2, y=3, changed=True)
    assert target_scroll_position(
        offsets.starts,
        (2, 1, 3),
        row=1,
        current_y=0,
        viewport_height=3,
        content_height=offsets.content_height,
    ) == ScrollTarget(row=0, y=0, changed=False)
