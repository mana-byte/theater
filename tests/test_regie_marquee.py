"""Pure participant-description marquee behaviour."""

from __future__ import annotations

from rich.cells import cell_len

from theater.regie.animations.marquee import clip_cells, marquee_cells, overflows_cells


def test_clip_cells_never_splits_a_wide_character():
    assert clip_cells("界abc", 1) == ""
    assert clip_cells("界abc", 3) == "界a"


def test_marquee_is_bounded_and_scrolls_continuously_left_across_three_spaces():
    frames = [marquee_cells("abcd", 3, frame, pause_frames=1) for frame in range(8)]
    assert frames == ["abc", "bcd", "cd ", "d  ", "   ", "  a", " ab", "abc"]
    assert all(cell_len(frame) <= 3 for frame in frames)


def test_marquee_pauses_before_starting_and_after_each_full_cycle():
    assert [marquee_cells("abcd", 3, frame) for frame in range(8)] == ["abc"] * 8
    assert marquee_cells("abcd", 3, 8) == "bcd"
    assert [marquee_cells("abcd", 3, frame) for frame in range(14, 22)] == ["abc"] * 8
    assert marquee_cells("abcd", 3, 22) == "bcd"


def test_fitting_text_is_stable_at_every_offset():
    assert not overflows_cells("short", 5)
    assert marquee_cells("short", 5, 0) == "short"
    assert marquee_cells("short", 5, 99) == "short"
