"""The pixel cat: it eats, drinks, hops off to make its bed and nap, breathing and snoring."""

from __future__ import annotations

import random

from regie.ambience.pixels import HALF_BLOCKS
from regie.ambience.scene import Cell, Phase
from regie.ambience.scenes.cat import CatScene

DT = 1 / CatScene.fps
Frame = tuple[str, tuple[int, int], tuple[int, int], list[Cell]]


def _life(seed: int, minutes: float) -> list[Frame]:
    cat = CatScene(random.Random(seed))
    cat.resize(46, 14)
    frames = []
    for i in range(int(minutes * 60 * CatScene.fps)):
        frames.append((cat.state, cat.bed, cat.food, cat.frame(Phase.IDLE, i * DT, DT)))
    return frames


def test_the_cat_eats_drinks_and_hops_off_to_nap_on_its_bed_wherever_it_lands() -> None:
    frames = _life(3, 8)
    assert {"sit", "hop", "eat", "drink", "sleep"} <= {state for state, *_ in frames}
    assert len({bed for state, bed, _, _ in frames if state == "sleep"}) >= 2  # the bed moves
    assert len({food for _, _, food, _ in frames}) >= 2  # an emptied bowl is put elsewhere
    snores = {c.glyph for state, _, _, cells in frames if state == "sleep" for c in cells}
    assert {"z", "Z"} <= snores <= {"z", "Z", *HALF_BLOCKS}  # pixels, and snores


def test_the_cat_breathes_chews_laps_and_swishes_its_tail() -> None:
    frames = _life(3, 8)
    for state in ("sit", "eat", "drink", "sleep"):
        looks = {tuple(cells) for s, _, _, cells in frames if s == state}
        assert len(looks) > 2, state  # never a still picture


def test_a_band_too_short_for_the_cat_stays_empty() -> None:
    cat = CatScene(random.Random(1))
    cat.resize(46, 9)
    assert cat.frame(Phase.IDLE, 0.0, DT) == []
