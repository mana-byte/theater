"""The status-change sweep: which changes earn one, how it is tinted, and its timing."""

from __future__ import annotations

import pytest
from regie.motions.wipe import LeafWipeController, apply_wipe, status_sweep
from regie.ui_constants import REGIE_WIPE_DEATH_SECONDS, REGIE_WIPE_SECONDS
from textual.content import Content

KEY = ("p", "first")


@pytest.mark.parametrize(
    ("previous", "status", "sweep"),
    [
        ("working", "awaiting_input", ("$warning", None)),
        ("idle", "awaiting_input", ("$warning", None)),
        ("working", "dead", ("$error", REGIE_WIPE_DEATH_SECONDS)),
        ("working", "idle", ("$success", None)),
        ("idle", "working", None),  # the spinner starting is announcement enough
        ("awaiting_input", "idle", None),
        ("dead", "idle", None),
    ],
)
def test_only_changes_that_matter_earn_a_sweep(previous, status, sweep):
    assert status_sweep(previous, status) == sweep


def _marks(content: Content, style: str) -> list[tuple[int, int]]:
    """Column ranges on the first row carrying *style*."""
    first = len(content.plain.split("\n")[0])
    return [
        (span.start, min(span.end, first))
        for span in content.spans
        if str(span.style) == style and span.start < first
    ]


def _first_row(content: Content) -> str:
    return content.plain.split("\n")[0]


def test_a_reversed_block_leads_and_runs_right():
    content = Content.assemble("├── abc\n", "│   ~/p")

    def block(progress: float):
        return _marks(apply_wipe(content, (progress, "$warning"), width=20), "reverse $warning")

    early, late = block(0.3), block(0.6)
    assert early and late
    assert late[0][0] > early[0][0]


def test_on_an_ansi_theme_the_trail_is_shade_blocks_thinning_away_from_the_block():
    swept = apply_wipe(Content("├── abc"), (0.6, "$warning"), width=30)

    (block,) = _marks(swept, "reverse $warning")
    row = _first_row(swept)
    assert row[block[0] - 9 : block[0]] == "░░░▒▒▒▓▓▓"  # nine cells, densest next to the block
    assert all("on " not in str(span.style) for span in swept.spans)  # an ANSI theme can't blend


def test_on_an_ansi_theme_text_under_the_trail_stays_and_fades_by_weight():
    swept = apply_wipe(Content("abcdefghijklmnopqrstuvwxyz"), (0.6, "$warning"), width=26)

    (block,) = _marks(swept, "reverse $warning")
    assert _first_row(swept)[: block[0]] == "abcdefghijklmnopqrstuvwxyz"[: block[0]]

    def cells(style: str) -> set[int]:
        return {col for start, stop in _marks(swept, style) for col in range(start, stop)}

    nearest = block[0]
    assert cells("bold $warning") == set(range(nearest - 3, nearest))
    assert cells("$warning") == set(range(nearest - 6, nearest - 3))
    assert cells("dim $warning") == set(range(nearest - 9, nearest - 6))


def test_on_a_truecolor_theme_the_trail_is_blocks_of_fading_opacity_over_anything():
    content = Content("├── abc")
    swept = apply_wipe(content, (0.6, "$warning"), width=30, blend=True)

    (block,) = _marks(swept, "reverse $warning")
    alphas = {
        span.start: int(str(span.style).split()[-1].rstrip("%"))
        for span in swept.spans
        if str(span.style).startswith("on $warning ")
    }
    cells = [block[0] - 1 - behind for behind in range(9)]  # nearest the block first
    fading = [alphas[cell] for cell in cells]
    assert fading == sorted(fading, reverse=True) and len(set(fading)) == len(fading)
    assert fading[0] < 100 and fading[-1] > 0
    assert _first_row(swept)[: len(content.plain)] == "├── abc"  # the text is untouched


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
