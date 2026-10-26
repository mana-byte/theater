"""Text motion: the participant-description marquee and the cycling sentence and tip controllers."""

from __future__ import annotations

from regie.motions.cycling_text import (
    HOLDING,
    TYPING_IN,
    TYPING_OUT,
    CyclingTextController,
    CyclingWindowController,
)
from regie.motions.marquee import clip_cells, marquee_cells, overflows_cells
from rich.cells import cell_len


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


class _PredictableRandom:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)

    def randrange(self, *_args: int) -> int:
        return next(self._values)


def test_cycling_text_types_in_holds_and_types_out():
    controller = CyclingTextController(
        [("hello ", ("world", "$accent bold"))], hold=5.0, char_interval=0.1
    )
    assert controller.total_length == 11
    for visible in range(1, 12):
        frame = controller.tick()
        assert frame.phase == (HOLDING if visible == 11 else TYPING_IN)
        assert frame.visible == visible
    assert frame.next_delay == 5.0
    frame = controller.tick()
    assert frame.phase == TYPING_OUT
    frame = controller.tick()
    assert frame.visible == 10


def test_cycling_text_advances_and_wraps_after_erasing():
    controller = CyclingTextController([("a",), ("b",)], hold=0.01, char_interval=0.1)
    controller.tick()
    controller.tick()
    controller.tick()
    assert controller.index == 1
    controller.tick()
    controller.tick()
    controller.tick()
    assert controller.index == 0


def test_manual_advance_resets_typing_state_and_wraps():
    controller = CyclingTextController([("first",), ("second",)], hold=1.0, char_interval=0.1)
    controller.tick()
    controller.tick()
    assert controller.visible == 2
    assert controller.advance()
    assert controller.index == 1
    assert controller.phase == TYPING_IN
    assert controller.visible == 0
    assert controller.advance()
    assert controller.index == 0


def test_manual_advance_is_safe_for_empty_corpus():
    controller = CyclingTextController([], hold=1.0, char_interval=0.1)
    assert not controller.active
    assert not controller.advance()
    assert controller.parts == ()
    assert controller.tick().cursor is False


def test_randomized_cycle_starts_randomly_and_never_repeats_current_item():
    controller = CyclingTextController(
        [("a",), ("b",), ("c",), ("d",)],
        hold=1.0,
        char_interval=0.1,
        randomize=True,
        rng=_PredictableRandom(1, 2, 3),
    )
    assert controller.index == 1
    controller.advance()
    assert controller.index == 3
    controller.advance()
    assert controller.index == 2


def test_randomized_single_item_cycle_is_safe():
    controller = CyclingTextController(
        [("only",)],
        hold=1.0,
        char_interval=0.1,
        randomize=True,
    )
    assert controller.advance()
    assert controller.index == 0


def test_cursor_blinks_during_typing_and_disappears_during_hold():
    controller = CyclingTextController([("abcd",)], hold=10.0, char_interval=0.1)
    frames = [controller.tick() for _ in range(4)]
    assert [frame.cursor for frame in frames] == [True, False, True, False]
    assert frames[-1].phase == HOLDING


def test_cursor_blinks_during_typing_out():
    controller = CyclingTextController([("abc",)], hold=0.01, char_interval=0.1)
    for _ in range(4):
        controller.tick()
    assert [controller.tick().cursor for _ in range(2)] == [True, False]


def test_cycling_window_keeps_three_tips_visible_and_reveals_only_the_new_one():
    items = (("one",), ("two",), ("three",), ("four",))
    controller = CyclingWindowController(
        items,
        window_size=3,
        hold=6.0,
        char_interval=0.04,
    )

    assert controller.window == items[:3]
    assert controller.phase == HOLDING
    assert controller.incoming_visible is None
    assert controller.initial_delay == 6.0
    assert controller.reveal_delay == 0.04

    frame = controller.tick()

    assert controller.index == 1
    assert controller.window == items[1:]
    assert controller.incoming_visible == 1
    assert frame.phase == TYPING_IN
    assert frame.next_delay == 0.04


def test_cycling_window_wraps_and_handles_an_empty_corpus():
    controller = CyclingWindowController(
        (("one",), ("two",)),
        window_size=3,
        hold=6.0,
        char_interval=0.04,
    )
    assert controller.advance()
    assert controller.window == (("two",), ("one",))
    assert controller.advance()
    assert controller.index == 0

    empty = CyclingWindowController((), window_size=3, hold=6.0, char_interval=0.04)
    assert not empty.active
    assert not empty.advance()
    assert empty.tick().phase == HOLDING
