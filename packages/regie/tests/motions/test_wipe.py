"""The status-change sweep: which changes earn one, how it is tinted, and its timing."""

from __future__ import annotations

import pytest
from regie.motions.wipe import LeafWipeController, apply_wipe, status_wipe_color
from regie.ui_constants import REGIE_WIPE_SECONDS
from textual.content import Content

KEY = ("p", "first")


@pytest.mark.parametrize(
    ("previous", "status", "color"),
    [
        ("working", "awaiting_input", "$warning"),
        ("idle", "awaiting_input", "$warning"),
        ("working", "dead", "$error"),
        ("working", "idle", "$success"),
        ("idle", "working", None),  # the spinner starting is announcement enough
        ("awaiting_input", "idle", None),
        ("dead", "idle", None),
    ],
)
def test_only_changes_that_matter_earn_a_sweep(previous, status, color):
    assert status_wipe_color(previous, status) == color


def _marks(content: Content, style: str) -> list[tuple[int, int]]:
    """Column ranges on the first row carrying *style*."""
    first = len(content.plain.split("\n")[0])
    return [
        (span.start, min(span.end, first))
        for span in content.spans
        if str(span.style) == style and span.start < first
    ]


def test_a_reversed_block_leads_and_a_bold_wake_follows():
    content = Content.assemble("├── abc\n", "│   ~/p")

    def sweep(progress: float):
        swept = apply_wipe(content, (progress, "$warning"), width=20)
        return _marks(swept, "reverse $warning"), _marks(swept, "bold $warning")

    (early_block, early_wake), (late_block, late_wake) = sweep(0.3), sweep(0.6)
    assert early_block and late_block
    assert late_block[0][0] > early_block[0][0]  # the block runs right
    assert early_wake and early_wake[0][1] == early_block[0][0]  # the wake trails right behind it
    assert late_wake and late_wake[0][1] == late_block[0][0]


def test_the_sweep_only_uses_foreground_styles_an_ansi_theme_can_draw():
    swept = apply_wipe(Content("├── abc"), (0.5, "$error"), width=20)
    assert all("on " not in str(span.style) for span in swept.spans)


def test_every_row_is_padded_so_the_sweep_crosses_the_whole_leaf():
    content = Content.assemble("short\n", "a much longer second row")
    swept = apply_wipe(content, (0.5, "$error"), width=30)
    assert {len(line.plain) for line in swept.split("\n")} == {30}


def test_a_finished_sweep_leaves_nothing_visible():
    swept = apply_wipe(Content("row"), (0.999, "$error"), width=3)
    assert not [span for span in swept.spans if span.start < span.end]


def test_the_sweep_stays_within_the_row_even_past_its_end():
    swept = apply_wipe(Content("ab"), (0.95, "$error"), width=2)
    assert all(span.end <= len(swept.plain) for span in swept.spans)


def test_controller_runs_a_sweep_to_completion():
    controller = LeafWipeController()
    controller.start(KEY, "$warning", now=0.0)
    assert controller.active

    frame = controller.tick(now=REGIE_WIPE_SECONDS / 2)
    assert frame.running == {KEY: (0.5, "$warning")}
    assert frame.active and not frame.finished

    frame = controller.tick(now=REGIE_WIPE_SECONDS)
    assert frame.running == {} and frame.finished == {KEY}
    assert not frame.active


def test_a_new_sweep_replaces_the_one_in_flight():
    controller = LeafWipeController()
    controller.start(KEY, "$warning", now=0.0)
    controller.start(KEY, "$error", now=0.4)
    assert controller.tick(now=0.45).running[KEY][1] == "$error"


def test_a_departed_leaf_is_cancelled_silently():
    controller = LeafWipeController()
    controller.start(KEY, "$warning", now=0.0)
    controller.cancel({KEY})
    assert not controller.active
    assert controller.tick(now=0.1).finished == frozenset()


def test_disabled_controller_never_sweeps():
    controller = LeafWipeController(enabled=False)
    controller.start(KEY, "$warning", now=0.0)
    assert not controller.active


def test_a_sweep_can_name_its_own_length_and_a_replacement_takes_the_new_one():
    controller = LeafWipeController()
    controller.start(KEY, "$error", seconds=0.2, now=0.0)
    assert controller.tick(now=0.1).running == {KEY: (0.5, "$error")}
    assert controller.tick(now=0.2).finished == {KEY}

    controller.start(KEY, "$error", now=0.0)  # no length named: the default one
    assert controller.tick(now=0.2).running[KEY][0] == 0.2 / REGIE_WIPE_SECONDS
