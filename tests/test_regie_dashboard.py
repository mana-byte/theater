"""Focused tests for dashboard text content and animation state."""

from __future__ import annotations

from theater.constants.regie import (
    REGIE_DASHBOARD_SENTENCES,
    REGIE_DASHBOARD_TIP_CURSOR_STYLE,
    REGIE_DASHBOARD_TIPS,
)
from theater.regie.animations.cycling_text import (
    HOLDING,
    TYPING_IN,
    TYPING_OUT,
    CyclingTextController,
    CyclingWindowController,
)
from theater.regie.dashboard.content import (
    animated_text_content,
    dashboard_tip_window_content,
    harness_availability_content,
    sentence_parts,
)


def _text(parts) -> str:
    return "".join(part if isinstance(part, str) else part[0] for part in parts)


def _styles(parts) -> list[str]:
    return [part[1] for part in parts if isinstance(part, tuple)]


class _PredictableRandom:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)

    def randrange(self, *_args: int) -> int:
        return next(self._values)


def test_sentence_corpus_has_at_least_30_distinct_highlighted_entries():
    texts = [_text(parts) for parts in REGIE_DASHBOARD_SENTENCES]
    assert len(texts) >= 30
    assert len(set(texts)) == len(texts)
    assert all(texts)
    highlighted = (
        any("accent" in style for style in _styles(parts)) for parts in REGIE_DASHBOARD_SENTENCES
    )
    assert all(highlighted)


def test_sentence_corpus_highlights_imagine_and_building_where_present():
    for parts in REGIE_DASHBOARD_SENTENCES:
        text = _text(parts)
        highlighted = [part[0] for part in parts if isinstance(part, tuple)]
        if "imagine" in text:
            assert "imagine" in highlighted
        if "building" in text:
            assert "building" in highlighted


def test_tip_corpus_uses_categories_and_highlighted_actions():
    assert len(REGIE_DASHBOARD_TIPS) >= 30
    for parts in REGIE_DASHBOARD_TIPS:
        assert " · " in _text(parts)
        styles = _styles(parts)
        assert any("text-muted" in style for style in styles)
        assert any("accent" in style for style in styles)


def test_tip_corpus_covers_regie_capabilities():
    corpus = "\n".join(_text(parts) for parts in REGIE_DASHBOARD_TIPS)
    for phrase in (
        "h/j/k/l",
        "past the last agent",
        "Enter",
        "H/L",
        "<prefix> h",
        "left-click",
        "right-click",
        "usage tile",
        "day, week, and month",
        "per-model details",
        "Ctrl+P",
        "fuzzy search",
        "diagnostic views",
        "duration order",
        "live tail",
        "copies the selected span",
        "lost trajectory",
        "participant link",
        "collapse or expand",
        "Spawn",
        "Resume sessions",
        "coordination traffic",
        "color scheme",
        "SVG screenshot",
        "kills",
        "daemon and agent sessions keep running",
        "regie.cost_window",
        "click this panel",
    ):
        assert phrase in corpus


def test_configured_sentences_replace_the_builtin_corpus_and_may_disable_it():
    assert sentence_parts(["make it clear", "keep it small"]) == (
        ("make it clear",),
        ("keep it small",),
    )
    assert sentence_parts([]) == ()


def test_harness_availability_uses_compact_marks_and_muted_failures():
    content = harness_availability_content(
        [
            {"name": "codex", "installed": True, "error": None},
            {"name": "vibe", "installed": False, "error": None},
            {"name": "broken", "installed": True, "error": "import failed"},
        ]
    )
    assert str(content) == "✓ codex\n✗ vibe\n✗ broken"
    assert [span.style for span in content.spans] == [
        "$success",
        "$text-muted",
        "$text-muted",
    ]


def test_pi_harness_availability_shows_beta_flag():
    content = harness_availability_content(
        [{"name": "pi", "installed": True, "error": None}]
    )

    assert str(content) == "✓ pi β"
    assert [span.style for span in content.spans] == ["$success", "$warning dim"]


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


def test_resume_delay_matches_current_phase():
    controller = CyclingTextController([("a",)], hold=7.0, char_interval=0.04)
    assert controller.resume_delay == 0.04
    controller.tick()
    assert controller.phase == HOLDING
    assert controller.resume_delay == 7.0
    controller.tick()
    assert controller.phase == TYPING_OUT
    assert controller.resume_delay == 0.04


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


def test_animated_content_preserves_partial_highlight():
    parts = ("imagine ", ("writing", "$accent bold"), " code")
    content = animated_text_content(parts, 9)
    assert str(content) == "imagine w"
    assert any("accent" in span.style for span in content.spans if span.style)


def test_animated_content_places_cursor_after_visible_prefix():
    parts = ("imagine ", ("writing", "$accent bold"), " code")
    content = animated_text_content(parts, 9, cursor=True)
    assert str(content) == "imagine w█"


def test_animated_content_accepts_dim_tip_cursor_style():
    content = animated_text_content(
        (("Tips: ", "$text-muted dim"), ("click", "$text-accent bold")),
        6,
        cursor=True,
        cursor_style=REGIE_DASHBOARD_TIP_CURSOR_STYLE,
    )
    assert str(content) == "Tips: █"
    assert any("text-muted" in span.style and "dim" in span.style for span in content.spans)


def test_animated_content_at_zero_is_blank_without_cursor():
    assert str(animated_text_content(("hello",), 0)) == ""


def test_tip_window_renders_one_active_and_two_dimmed_lines():
    content = dashboard_tip_window_content(
        (
            (("Tree · ", "$text-muted bold"), ("move", "$text-accent bold")),
            (("Usage · ", "$text-muted bold"), ("inspect", "$text-accent bold")),
            (("Trajectory · ", "$text-muted bold"), ("search", "$text-accent bold")),
        ),
        incoming_visible=13,
        cursor=True,
    )

    assert str(content) == "Tips\n› Tree · move\n  Usage · inspect\n  Trajectory · █"
    styles = [span.style for span in content.spans if span.style]
    assert any("accent" in style and "dim" not in style for style in styles)
    assert any("accent" in style and "dim" in style for style in styles)
