"""The footer ambience: a quiet line that grows in on the band's bottom row."""

from __future__ import annotations

from itertools import pairwise

from regie.ambience.driver import AmbienceDriver
from regie.ambience.scene import Cell, Phase
from regie.ambience.scenes.footer import FooterScene

DT = 1 / FooterScene.fps


def _driver(width: int = 40, height: int = 4) -> AmbienceDriver:
    driver = AmbienceDriver(FooterScene, seed=1)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _play_intro(driver: AmbienceDriver) -> list[list[Cell]]:
    frames = []
    while driver.phase is Phase.INTRO:
        frames.append(driver.tick(DT))
    return frames


def test_the_intro_grows_the_line_from_the_centre_across_the_bottom_row() -> None:
    frames = _play_intro(_driver())
    first, last = frames[0], frames[-1]
    assert {cell.y for cell in first} == {3}  # the band's bottom row only
    assert len(first) < len(last) == 40
    assert min(cell.x for cell in first) == (40 - len(first)) // 2  # centred, not left-anchored


def test_the_idle_line_breathes_rarely_and_stays_on_the_bottom_row() -> None:
    driver = _driver()
    _play_intro(driver)
    frames = [driver.tick(DT) for _ in range(int(12 * FooterScene.fps))]
    assert all(len(frame) == 40 and {cell.y for cell in frame} == {3} for frame in frames)
    breathes = [frame for frame in frames if frame[0].glyph == "="]
    assert 0 < len(breathes) < len(frames) / 2  # a rare character shift, never a fast flicker
    assert all(cell.glyph in ("-", "=") for frame in frames for cell in frame)


def test_the_outro_shrinks_the_line_away_to_nothing() -> None:
    driver = _driver()
    _play_intro(driver)
    driver.set_active(False)
    sizes = [
        len(driver.tick(DT)) for _ in range(int(FooterScene.outro_seconds * FooterScene.fps) + 2)
    ]
    assert all(a >= b for a, b in pairwise(sizes))  # it shrinks, never jumps
    assert driver.tick(DT) == [] and not driver.running


def test_a_one_row_band_draws_on_its_only_row() -> None:
    driver = _driver(width=20, height=1)
    frames = _play_intro(driver)
    assert frames and all(cell.y == 0 for frame in frames for cell in frame)
    assert frames[-1] and len(frames[-1]) == 20
    driver.set_active(False)
    while driver.running:
        driver.tick(DT)
    assert driver.tick(DT) == []
