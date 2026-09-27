"""The pixel cat: centred at the bottom, it sits, eats, drinks and naps, never still."""

from __future__ import annotations

import random

from regie.ambience.pixels import HALF_BLOCKS
from regie.ambience.scene import Cell, Phase
from regie.ambience.scenes.cat import CatScene

DT = 1 / CatScene.fps


def _life(seed: int, minutes: float) -> list[tuple[str, list[Cell]]]:
    cat = CatScene(random.Random(seed))
    cat.resize(46, 14)
    return [(cat.state, cat.frame(Phase.IDLE, i * DT, DT)) for i in range(int(minutes * 480))]


def test_the_cat_does_everything_centred_at_the_bottom_of_the_band() -> None:
    frames = _life(3, 5)
    assert {"sit", "eat", "drink", "sleep"} <= {state for state, _ in frames}
    for state, cells in frames:
        pixels = [c for c in cells if c.glyph in HALF_BLOCKS]
        middle = (min(c.x for c in pixels) + max(c.x for c in pixels)) / 2
        assert max(c.y for c in pixels) == 13 and abs(middle - 23) <= 4, state
    snores = {c.glyph for state, cells in frames if state == "sleep" for c in cells}
    assert {"z", "Z"} <= snores


def test_the_cat_breathes_chews_laps_and_swishes_its_tail() -> None:
    frames = _life(3, 5)
    for state in ("sit", "eat", "drink", "sleep"):
        looks = {tuple(cells) for s, cells in frames if s == state}
        assert len(looks) > 2, state  # never a still picture


def test_a_band_too_short_for_the_cat_stays_empty() -> None:
    cat = CatScene(random.Random(1))
    cat.resize(46, 9)
    assert cat.frame(Phase.IDLE, 0.0, DT) == []
