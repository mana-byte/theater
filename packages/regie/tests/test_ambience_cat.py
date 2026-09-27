"""The cat scene: it eats, drinks, hops off to make its bed and nap, breathing and snoring."""

from __future__ import annotations

import random

from regie.ambience.scene import Cell, Phase
from regie.ambience.scenes.cat import CatScene

DT = 1 / CatScene.fps
Frame = tuple[str, tuple[int, int], list[Cell]]


def _life(seed: int, minutes: float) -> list[Frame]:
    cat = CatScene(random.Random(seed))
    cat.resize(46, 10)
    frames = []
    for i in range(int(minutes * 60 * CatScene.fps)):
        frames.append((cat.state, cat.bed, cat.frame(Phase.IDLE, i * DT, DT)))
    return frames


def test_the_cat_eats_drinks_and_hops_off_to_nap_on_its_bed_wherever_it_lands() -> None:
    frames = _life(3, 8)
    assert {"sit", "hop", "eat", "drink", "sleep"} <= {state for state, _, _ in frames}
    assert len({bed for state, bed, _ in frames if state == "sleep"}) >= 2  # the bed moves
    snores = {c.glyph for state, _, cells in frames if state == "sleep" for c in cells}
    assert {"z", "Z"} <= snores


def test_the_cat_breathes_chews_laps_and_swishes_its_tail() -> None:
    frames = _life(3, 8)
    for state in ("sit", "eat", "drink", "sleep"):
        looks = {
            frozenset((c.x, c.y, c.glyph) for c in cells) for s, _, cells in frames if s == state
        }
        assert len(looks) > 2, state  # never a still picture
    glyphs = {
        s: {c.glyph for f, _, cells in frames if f == s for c in cells} for s in ("eat", "drink")
    }
    assert "o" in glyphs["eat"] and "u" in glyphs["drink"]  # the jaw bites, the tongue laps


def test_a_band_too_small_for_the_cat_stays_empty() -> None:
    cat = CatScene(random.Random(1))
    cat.resize(20, 10)
    assert cat.frame(Phase.IDLE, 0.0, DT) == []
