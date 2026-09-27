"""The stars scene: appearing one by one, twinkling in place, a shooting star."""

from __future__ import annotations

from itertools import dropwhile, takewhile

from regie.ambience.driver import AmbienceDriver
from regie.ambience.scene import Phase
from regie.ambience.scenes.stars import StarsScene

DT = 1 / StarsScene.fps


def _driver(width: int = 30, height: int = 6) -> AmbienceDriver:
    driver = AmbienceDriver(StarsScene, seed=11)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _reach_idle(driver: AmbienceDriver) -> None:
    while driver.phase is Phase.INTRO:
        driver.tick(DT)


def test_stars_appear_one_by_one() -> None:
    driver = _driver()
    frames = []
    while driver.phase is Phase.INTRO:
        frames.append(driver.tick(DT))
    assert driver.phase is Phase.IDLE
    counts = [len(frame) for frame in frames]
    assert counts == sorted(counts) and counts[0] < counts[-1]  # one by one, never fewer


def test_stars_twinkle_but_never_move() -> None:
    driver = _driver()
    _reach_idle(driver)
    frames = [driver.tick(DT) for _ in range(12)]
    seats = {(cell.x, cell.y) for frame in frames for cell in frame}
    assert seats == {(cell.x, cell.y) for cell in frames[0]}  # positions are stable
    looks = {tuple(sorted((cell.x, cell.y, cell.glyph) for cell in frame)) for frame in frames}
    assert len(looks) > 1  # a few stars change brightness frame to frame


def test_a_shooting_star_crosses_the_sky_rarely() -> None:
    driver = _driver(width=40, height=8)
    _reach_idle(driver)
    frames = [driver.tick(DT) for _ in range(int(4 * StarsScene.fps * 15))]  # a minute of idle
    streaks = [frame for frame in frames if any(cell.style == "$accent" for cell in frame)]
    assert streaks and all(sum(cell.style == "$accent" for cell in frame) == 1 for frame in streaks)
    assert len(streaks) < len(frames) / 2  # rare: mostly just the quiet sky


def test_stars_fade_out_one_by_one_on_blur() -> None:
    driver = _driver()
    _reach_idle(driver)
    driver.set_active(False)
    frames = []
    while driver.running:
        frames.append(driver.tick(DT))
    counts = [len(frame) for frame in frames]
    assert counts[-1] == 0 and counts == sorted(counts, reverse=True)


def test_a_ship_drifts_by_rarely_and_the_star_whale_weaves_through_the_sky() -> None:
    def visitors(height: int) -> tuple[set[str], int, int, set[int]]:
        driver = _driver(width=40, height=height)
        _reach_idle(driver)
        frames = [driver.tick(DT) for _ in range(int(StarsScene.fps * 60 * 12))]
        seen = [{c.style for c in f} & {"$warning", "$primary"} for f in frames]
        whale = [[c.y for c in f if c.style == "$primary"] for f in frames]
        crossing = takewhile(bool, dropwhile(lambda rows: not rows, whale))  # its first pass
        tops = {min(rows) for rows in crossing}
        return set().union(*seen), sum(map(bool, seen)), len(frames), tops

    kinds, busy, total, tops = visitors(10)
    assert kinds == {"$warning", "$primary"} and busy < total / 2  # a ship, a whale, rarely
    assert len(tops) >= 4  # in one pass the whale rises and dives, never a straight line
    assert visitors(3)[0] == {"$warning"}  # too short for the whale: only ships
