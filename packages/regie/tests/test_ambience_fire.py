"""The fire scene: kindling, a flickering low burn, and a dying outro."""

from __future__ import annotations

from regie.ambience.driver import AmbienceDriver
from regie.ambience.scene import Phase
from regie.ambience.scenes.fire import FireScene

DT = 1 / FireScene.fps


def _driver(width: int = 24, height: int = 6) -> AmbienceDriver:
    driver = AmbienceDriver(FireScene, seed=3)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _reach_idle(driver: AmbienceDriver) -> None:
    while driver.phase is Phase.INTRO:
        driver.tick(DT)


def test_fire_kindles_from_the_bottom_of_the_band() -> None:
    driver = _driver()
    frames = [driver.tick(DT) for _ in range(int(FireScene.fps * FireScene.intro_seconds))]
    assert driver.phase is Phase.IDLE
    assert frames[0] == []  # nothing is lit the instant focus arrives
    lit = driver.tick(DT)
    assert len(lit) > 0
    assert all(cell.y >= 4 for frame in [*frames, lit] for cell in frame)  # stays low


def test_fire_flickers_while_idle_but_never_climbs() -> None:
    driver = _driver()
    _reach_idle(driver)
    frames = [driver.tick(DT) for _ in range(6)]
    assert all(cell.y >= 4 for frame in frames for cell in frame)
    looks = {tuple(sorted((cell.x, cell.y, cell.glyph) for cell in frame)) for frame in frames}
    assert len(looks) > 1  # the burn flickers frame to frame


def test_fire_dies_down_to_embers_then_nothing() -> None:
    driver = _driver()
    _reach_idle(driver)
    driver.set_active(False)
    frames = []
    while driver.running:
        frames.append(driver.tick(DT))
    assert frames[-1] == []
    dying = [frame for frame in frames if frame]
    assert dying and all(cell.glyph == "." for cell in dying[-1])  # embers, then nothing


def test_fire_adapts_to_a_shrinking_band() -> None:
    driver = _driver(width=24, height=8)
    _reach_idle(driver)
    driver.set_band(10, 2)
    assert driver.phase is Phase.IDLE
    frames = [driver.tick(DT) for _ in range(4)]
    assert all(
        frame and all(cell.x < 10 and cell.y == 1 for cell in frame) for frame in frames
    )  # two rows left: the bottom row only
