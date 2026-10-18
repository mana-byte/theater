"""The leaves ambience: a sparse drift of leaves through the band under the tree."""

from __future__ import annotations

from itertools import pairwise

from regie.ambience.builtin.leaves.scene import LeavesScene
from regie.ambience.driver import AmbienceDriver
from regie.ambience.scene import Cell, Phase

DT = 1 / LeavesScene.fps
GLYPHS = {",", "'", "`", "*", "~"}
STYLES = {"$warning dim", "$success dim", "$text-muted"}


def _driver(width: int = 40, height: int = 8) -> AmbienceDriver:
    driver = AmbienceDriver(LeavesScene, seed=3)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _play_intro(driver: AmbienceDriver) -> list[list[Cell]]:
    frames = []
    while driver.phase is Phase.INTRO:
        frames.append(driver.tick(DT))
    return frames


def _run_idle(driver: AmbienceDriver, seconds: float) -> list[list[Cell]]:
    return [driver.tick(DT) for _ in range(int(seconds * LeavesScene.fps))]


def test_the_intro_ramps_up_and_idle_keeps_a_sparse_in_band_drift() -> None:
    driver = _driver()
    frames = _play_intro(driver)
    assert len(frames[0]) == 1  # the focus signal: a leaf is drawn within the first 0.4 s
    assert any(len(frame) > 1 for frame in frames)  # then the ramp brings more
    idle = _run_idle(driver, 10.0)
    target = max(1, 40 * 8 // 60)  # about one leaf per sixty cells
    for frame in frames + idle:
        assert all(cell.glyph in GLYPHS and cell.style in STYLES for cell in frame)
        assert all(0 <= cell.x < 40 and 0 <= cell.y < 8 for cell in frame)
        assert len(frame) <= target


def test_leaves_reach_the_bottom_row_and_linger_a_moment() -> None:
    driver = _driver()
    _play_intro(driver)
    frames = _run_idle(driver, 12.0)
    bottom = [i for i, frame in enumerate(frames) if any(cell.y == 7 for cell in frame)]
    assert bottom  # leaves do fall the whole band, not just its top
    assert bottom[-1] > bottom[0] + 4  # a landed leaf rests over several frames, then goes


def test_the_outro_spawns_nothing_and_empties_the_band() -> None:
    driver = _driver()
    _play_intro(driver)
    _run_idle(driver, 4.0)
    driver.set_active(False)
    sizes = [len(driver.tick(DT)) for _ in range(int(LeavesScene.outro_seconds * LeavesScene.fps))]
    assert all(a >= b for a, b in pairwise(sizes))  # existing leaves fade, nothing new falls
    assert driver.tick(DT) == [] and not driver.running


def test_leaves_never_burst_out_after_a_long_idle() -> None:
    driver = _driver()
    _play_intro(driver)
    sizes = [len(frame) for frame in _run_idle(driver, 30.0)]
    assert max(b - a for a, b in pairwise(sizes)) <= 1  # capped credit: at most one spawn per frame


def test_a_shrinking_band_adapts_its_drift() -> None:
    driver = _driver()
    _play_intro(driver)
    _run_idle(driver, 6.0)
    driver.set_band(12, 2)
    frames = _run_idle(driver, 6.0)
    assert frames
    for frame in frames:
        assert all(0 <= cell.x < 12 and 0 <= cell.y < 2 for cell in frame)
        assert len(frame) <= 1  # one leaf per sixty cells: a 12x2 band carries just one
