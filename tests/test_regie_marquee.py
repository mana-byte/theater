"""Pure participant-description marquee behaviour."""

from __future__ import annotations

from rich.cells import cell_len

from theater.regie.animations.marquee import clip_cells, marquee_cells, overflows_cells


def test_clip_cells_never_splits_a_wide_character():
    assert clip_cells("界abc", 1) == ""
    assert clip_cells("界abc", 3) == "界a"


def test_marquee_is_bounded_and_moves_right_to_left():
    first = marquee_cells("description", 5, 0)
    later = marquee_cells("description", 5, 1)
    assert first == "descr"
    assert later == "escri"
    assert cell_len(first) <= 5
    assert cell_len(later) <= 5


def test_fitting_text_is_stable_at_every_offset():
    assert not overflows_cells("short", 5)
    assert marquee_cells("short", 5, 0) == "short"
    assert marquee_cells("short", 5, 99) == "short"
