"""The fire scene: a campfire that kindles, burns in colour, smokes, and dies away."""

from __future__ import annotations

from itertools import pairwise

import pytest
from regie.ambience.driver import AmbienceDriver
from regie.ambience.scene import Cell, Phase
from regie.ambience.scenes.fire import FireScene

DT = 1 / FireScene.fps
SMOKE = {"(", ")", "~", "-"}


def _driver(width: int = 40, height: int = 12) -> AmbienceDriver:
    driver = AmbienceDriver(FireScene, seed=3)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _idle(driver: AmbienceDriver, seconds: float) -> list[list[Cell]]:
    while driver.phase is Phase.INTRO:
        driver.tick(DT)
    return [driver.tick(DT) for _ in range(int(seconds * FireScene.fps))]


@pytest.mark.parametrize("height", [12, 30])
def test_a_small_colourful_fire_hottest_only_at_its_base(height: int) -> None:
    frames = _idle(_driver(height=height), 5.0)
    flames = [cell for frame in frames for cell in frame if cell.glyph not in SMOKE]
    tallest = height - min(cell.y for cell in flames)
    assert 4 <= tallest <= 9  # a campfire, even in a tall band
    core = [cell for cell in flames if cell.glyph == "#"]
    assert len(core) < len(flames) / 3  # mostly flames, not a white-hot block
    assert {"ansi_bright_white", "ansi_bright_yellow", "ansi_yellow", "ansi_bright_red"} <= {
        cell.style.split()[-1] for cell in flames
    }  # white, yellows and reds, whatever the theme
    base = [cell for cell in flames if cell.y == height - 1]
    assert base and all(cell.glyph == "#" for cell in base)  # white-hot bed on the bottom row


def test_the_fire_smokes_above_its_flames_and_stays_off_the_edges() -> None:
    frames = _idle(_driver(), 10.0)
    smoke = [cell for frame in frames for cell in frame if cell.glyph in SMOKE]
    assert smoke and min(cell.y for cell in smoke) <= 1  # wisps rise to the top
    assert all("ansi_bright_black" in cell.style for cell in smoke)  # grey, never fire-coloured
    flames = [cell for frame in frames for cell in frame if cell.glyph not in SMOKE]
    assert not any("black" in cell.style for cell in flames)
    bed = [cell.x for frame in frames for cell in frame if cell.y == 11]
    assert max(bed) - min(bed) <= 16  # a campfire in the middle, not a wall of flame


def test_the_fire_dies_away_without_smoke_to_nothing() -> None:
    driver = _driver()
    _idle(driver, 3.0)
    driver.set_active(False)
    frames = []
    while driver.running:
        frames.append(driver.tick(DT))
    assert frames[-1] == []

    def bed(frame: list[Cell]) -> int:
        return sum(cell.y == 11 for cell in frame)

    third = len(frames) // 3
    early, late = frames[:third], frames[-third:]
    assert sum(map(bed, late)) / len(late) < sum(map(bed, early)) / len(early) / 2  # dies down
    smoke = [len([c for c in frame if c.glyph in SMOKE]) for frame in frames]
    assert all(later <= earlier for earlier, later in pairwise(smoke))


def test_a_two_row_band_keeps_a_low_fire_without_smoke() -> None:
    driver = _driver(width=24, height=8)
    _idle(driver, 1.0)
    driver.set_band(10, 2)
    frames = [driver.tick(DT) for _ in range(8)]
    assert any(frames)
    assert all(cell.x < 10 and cell.glyph not in SMOKE for frame in frames for cell in frame)
