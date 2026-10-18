"""The shipped ambience scenes: aquarium, fire, footer, leaves and stars."""

from __future__ import annotations

import random
from itertools import pairwise

import pytest
from regie.ambience.builtin.aquarium.scene import AquariumScene
from regie.ambience.builtin.fire.scene import FireScene
from regie.ambience.builtin.footer.scene import FooterScene
from regie.ambience.builtin.leaves.scene import LeavesScene
from regie.ambience.builtin.stars.scene import StarsScene
from regie.ambience.builtin.stars.whale import StarWhale
from regie.ambience.driver import AmbienceDriver
from regie.ambience.scene import Cell, Phase


# Aquarium
def _fish(cells: list[Cell]) -> dict[int, Cell]:
    return {cell.y: cell for cell in cells if "<" in cell.glyph or ">" in cell.glyph}


def test_fish_enter_then_swim_in_the_direction_their_sprite_faces() -> None:
    driver = AmbienceDriver(AquariumScene, seed=4)
    driver.set_band(30, 6)
    driver.set_active(True)

    entered = _fish(driver.tick(AquariumScene.intro_seconds))
    assert driver.phase is Phase.IDLE and len(entered) == 2
    moved = _fish(driver.tick(0.5))

    for lane, fish in entered.items():
        distance = moved[lane].x - fish.x
        assert distance > 0 if fish.glyph.startswith(">") else distance < 0


def test_idle_has_bottom_seaweed_and_bubbles_that_rise_before_the_top() -> None:
    driver = AmbienceDriver(AquariumScene, seed=7)
    driver.set_band(36, 6)
    driver.set_active(True)
    cells = driver.tick(AquariumScene.intro_seconds)
    assert any(cell.glyph in "()" and cell.y == 5 for cell in cells)

    first_bubble: Cell | None = None
    for _ in range(20):
        cells = driver.tick(0.2)
        first_bubble = next((cell for cell in cells if cell.glyph in ".oO"), None)
        if first_bubble is not None:
            break
    assert first_bubble is not None and first_bubble.y > 0

    rose = False
    for _ in range(10):
        cells = driver.tick(0.2)
        rose = rose or any(
            cell.x == first_bubble.x
            and cell.glyph == first_bubble.glyph
            and 0 < cell.y < first_bubble.y
            for cell in cells
        )
    assert rose


def test_shrink_uses_one_lane_without_seaweed_and_outro_empties() -> None:
    driver = AmbienceDriver(AquariumScene, seed=3)
    driver.set_band(32, 6)
    driver.set_active(True)
    driver.tick(AquariumScene.intro_seconds)
    driver.set_band(20, 2)

    frames = [driver.tick(0.2) for _ in range(10)]
    cells = [cell for frame in frames for cell in frame]
    assert cells and all(cell.y == 1 and cell.glyph not in "()" for cell in cells)

    driver.set_active(False)
    assert driver.tick(AquariumScene.outro_seconds) == []
    assert not driver.running


# Fire
FIRE_DT = 1 / FireScene.fps


SMOKE = {"(", ")", "~", "-"}


def _fire_driver(width: int = 40, height: int = 12) -> AmbienceDriver:
    driver = AmbienceDriver(FireScene, seed=3)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _fire_idle(driver: AmbienceDriver, seconds: float) -> list[list[Cell]]:
    while driver.phase is Phase.INTRO:
        driver.tick(FIRE_DT)
    return [driver.tick(FIRE_DT) for _ in range(int(seconds * FireScene.fps))]


@pytest.mark.parametrize("height", [12, 30])
def test_a_small_colourful_fire_hottest_only_at_its_base(height: int) -> None:
    frames = _fire_idle(_fire_driver(height=height), 5.0)
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
    frames = _fire_idle(_fire_driver(), 10.0)
    smoke = [cell for frame in frames for cell in frame if cell.glyph in SMOKE]
    assert smoke and min(cell.y for cell in smoke) <= 1  # wisps rise to the top
    assert all("ansi_bright_black" in cell.style for cell in smoke)  # grey, never fire-coloured
    flames = [cell for frame in frames for cell in frame if cell.glyph not in SMOKE]
    assert not any("black" in cell.style for cell in flames)
    bed = [cell.x for frame in frames for cell in frame if cell.y == 11]
    assert max(bed) - min(bed) <= 16  # a campfire in the middle, not a wall of flame


def test_the_fire_dies_away_without_smoke_to_nothing() -> None:
    driver = _fire_driver()
    _fire_idle(driver, 3.0)
    driver.set_active(False)
    frames = []
    while driver.running:
        frames.append(driver.tick(FIRE_DT))
    assert frames[-1] == []

    def bed(frame: list[Cell]) -> int:
        return sum(cell.y == 11 for cell in frame)

    third = len(frames) // 3
    early, late = frames[:third], frames[-third:]
    assert sum(map(bed, late)) / len(late) < sum(map(bed, early)) / len(early) / 2  # dies down
    smoke = [len([c for c in frame if c.glyph in SMOKE]) for frame in frames]
    assert all(later <= earlier for earlier, later in pairwise(smoke))


def test_a_two_row_band_keeps_a_low_fire_without_smoke() -> None:
    driver = _fire_driver(width=24, height=8)
    _fire_idle(driver, 1.0)
    driver.set_band(10, 2)
    frames = [driver.tick(FIRE_DT) for _ in range(8)]
    assert any(frames)
    assert all(cell.x < 10 and cell.glyph not in SMOKE for frame in frames for cell in frame)


# Footer
FOOTER_DT = 1 / FooterScene.fps


def _footer_driver(width: int = 40, height: int = 4) -> AmbienceDriver:
    driver = AmbienceDriver(FooterScene, seed=1)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _footer_intro(driver: AmbienceDriver) -> list[list[Cell]]:
    frames = []
    while driver.phase is Phase.INTRO:
        frames.append(driver.tick(FOOTER_DT))
    return frames


def test_the_intro_grows_the_line_from_the_centre_across_the_bottom_row() -> None:
    frames = _footer_intro(_footer_driver())
    first, last = frames[0], frames[-1]
    assert {cell.y for cell in first} == {3}  # the band's bottom row only
    assert len(first) < len(last) == 40
    assert min(cell.x for cell in first) == (40 - len(first)) // 2  # centred, not left-anchored


def test_the_idle_line_breathes_rarely_and_stays_on_the_bottom_row() -> None:
    driver = _footer_driver()
    _footer_intro(driver)
    frames = [driver.tick(FOOTER_DT) for _ in range(int(12 * FooterScene.fps))]
    assert all(len(frame) == 40 and {cell.y for cell in frame} == {3} for frame in frames)
    breathes = [frame for frame in frames if frame[0].glyph == "="]
    assert 0 < len(breathes) < len(frames) / 2  # a rare character shift, never a fast flicker
    assert all(cell.glyph in ("-", "=") for frame in frames for cell in frame)


def test_the_outro_shrinks_the_line_away_to_nothing() -> None:
    driver = _footer_driver()
    _footer_intro(driver)
    driver.set_active(False)
    sizes = [
        len(driver.tick(FOOTER_DT))
        for _ in range(int(FooterScene.outro_seconds * FooterScene.fps) + 2)
    ]
    assert all(a >= b for a, b in pairwise(sizes))  # it shrinks, never jumps
    assert driver.tick(FOOTER_DT) == [] and not driver.running


def test_a_one_row_band_draws_on_its_only_row() -> None:
    driver = _footer_driver(width=20, height=1)
    frames = _footer_intro(driver)
    assert frames and all(cell.y == 0 for frame in frames for cell in frame)
    assert frames[-1] and len(frames[-1]) == 20
    driver.set_active(False)
    while driver.running:
        driver.tick(FOOTER_DT)
    assert driver.tick(FOOTER_DT) == []


# Leaves
LEAVES_DT = 1 / LeavesScene.fps


LEAF_GLYPHS = {",", "'", "`", "*", "~"}


LEAF_STYLES = {"$warning dim", "$success dim", "$text-muted"}


def _leaves_driver(width: int = 40, height: int = 8) -> AmbienceDriver:
    driver = AmbienceDriver(LeavesScene, seed=3)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _leaves_intro(driver: AmbienceDriver) -> list[list[Cell]]:
    frames = []
    while driver.phase is Phase.INTRO:
        frames.append(driver.tick(LEAVES_DT))
    return frames


def _leaves_idle(driver: AmbienceDriver, seconds: float) -> list[list[Cell]]:
    return [driver.tick(LEAVES_DT) for _ in range(int(seconds * LeavesScene.fps))]


def test_the_intro_ramps_up_and_idle_keeps_a_sparse_in_band_drift() -> None:
    driver = _leaves_driver()
    frames = _leaves_intro(driver)
    assert len(frames[0]) == 1  # the focus signal: a leaf is drawn within the first 0.4 s
    assert any(len(frame) > 1 for frame in frames)  # then the ramp brings more
    idle = _leaves_idle(driver, 10.0)
    target = max(1, 40 * 8 // 60)  # about one leaf per sixty cells
    for frame in frames + idle:
        assert all(cell.glyph in LEAF_GLYPHS and cell.style in LEAF_STYLES for cell in frame)
        assert all(0 <= cell.x < 40 and 0 <= cell.y < 8 for cell in frame)
        assert len(frame) <= target


def test_leaves_reach_the_bottom_row_and_linger_a_moment() -> None:
    driver = _leaves_driver()
    _leaves_intro(driver)
    frames = _leaves_idle(driver, 12.0)
    bottom = [i for i, frame in enumerate(frames) if any(cell.y == 7 for cell in frame)]
    assert bottom  # leaves do fall the whole band, not just its top
    assert bottom[-1] > bottom[0] + 4  # a landed leaf rests over several frames, then goes


def test_the_outro_spawns_nothing_and_empties_the_band() -> None:
    driver = _leaves_driver()
    _leaves_intro(driver)
    _leaves_idle(driver, 4.0)
    driver.set_active(False)
    sizes = [
        len(driver.tick(LEAVES_DT)) for _ in range(int(LeavesScene.outro_seconds * LeavesScene.fps))
    ]
    assert all(a >= b for a, b in pairwise(sizes))  # existing leaves fade, nothing new falls
    assert driver.tick(LEAVES_DT) == [] and not driver.running


def test_leaves_never_burst_out_after_a_long_idle() -> None:
    driver = _leaves_driver()
    _leaves_intro(driver)
    sizes = [len(frame) for frame in _leaves_idle(driver, 30.0)]
    assert max(b - a for a, b in pairwise(sizes)) <= 1  # capped credit: at most one spawn per frame


def test_a_shrinking_band_adapts_its_drift() -> None:
    driver = _leaves_driver()
    _leaves_intro(driver)
    _leaves_idle(driver, 6.0)
    driver.set_band(12, 2)
    frames = _leaves_idle(driver, 6.0)
    assert frames
    for frame in frames:
        assert all(0 <= cell.x < 12 and 0 <= cell.y < 2 for cell in frame)
        assert len(frame) <= 1  # one leaf per sixty cells: a 12x2 band carries just one


# Stars
STARS_DT = 1 / StarsScene.fps


def _stars_driver(width: int = 30, height: int = 6) -> AmbienceDriver:
    driver = AmbienceDriver(StarsScene, seed=11)
    driver.set_band(width, height)
    driver.set_active(True)
    return driver


def _stars_idle(driver: AmbienceDriver) -> None:
    while driver.phase is Phase.INTRO:
        driver.tick(STARS_DT)


def test_stars_appear_one_by_one() -> None:
    driver = _stars_driver()
    frames = []
    while driver.phase is Phase.INTRO:
        frames.append(driver.tick(STARS_DT))
    assert driver.phase is Phase.IDLE
    counts = [len(frame) for frame in frames]
    assert counts == sorted(counts) and counts[0] < counts[-1]  # one by one, never fewer


def test_stars_twinkle_but_never_move() -> None:
    driver = _stars_driver()
    _stars_idle(driver)
    frames = [driver.tick(STARS_DT) for _ in range(12)]
    seats = {(cell.x, cell.y) for frame in frames for cell in frame}
    assert seats == {(cell.x, cell.y) for cell in frames[0]}  # positions are stable
    looks = {tuple(sorted((cell.x, cell.y, cell.glyph) for cell in frame)) for frame in frames}
    assert len(looks) > 1  # a few stars change brightness frame to frame


def test_a_shooting_star_crosses_the_sky_rarely() -> None:
    driver = _stars_driver(width=40, height=8)
    _stars_idle(driver)
    frames = [
        driver.tick(STARS_DT) for _ in range(int(4 * StarsScene.fps * 15))
    ]  # a minute of idle
    streaks = [frame for frame in frames if any(cell.style == "$accent" for cell in frame)]
    assert streaks and all(sum(cell.style == "$accent" for cell in frame) == 1 for frame in streaks)
    assert len(streaks) < len(frames) / 2  # rare: mostly just the quiet sky


def test_stars_fade_out_one_by_one_on_blur() -> None:
    driver = _stars_driver()
    _stars_idle(driver)
    driver.set_active(False)
    frames = []
    while driver.running:
        frames.append(driver.tick(STARS_DT))
    counts = [len(frame) for frame in frames]
    assert counts[-1] == 0 and counts == sorted(counts, reverse=True)


def test_a_ship_drifts_by_rarely_and_the_star_whale_needs_room() -> None:
    def visitors(height: int) -> tuple[set[str], int, int]:
        driver = _stars_driver(width=40, height=height)
        _stars_idle(driver)
        frames = [driver.tick(STARS_DT) for _ in range(int(StarsScene.fps * 60 * 15))]
        seen = [{c.style for c in f} & {"$warning", "$primary"} for f in frames]
        return set().union(*seen), sum(map(bool, seen)), len(frames)

    kinds, busy, total = visitors(10)
    assert kinds == {"$warning", "$primary"} and busy < total / 2  # a ship, a whale, rarely
    assert visitors(5)[0] == {"$warning"}  # too short for the whale: only ships


def _feeding(whale_seed: int) -> tuple[StarsScene, float, list[int]]:
    """The same 40x10 sky, a whale let loose in it: when it left, and its path until then."""
    scene = StarsScene(random.Random(1))
    scene.resize(40, 10)
    whale = scene._visitor = StarWhale(random.Random(whale_seed), 40, 10)
    idle, path = 0.0, []
    while scene._visitor is not None and idle < StarWhale.max_feeding + 60:
        scene.frame(Phase.IDLE, idle, STARS_DT)
        idle, path = idle + STARS_DT, [*path, round(whale.y)]
    return scene, idle, path


def test_the_star_whale_eats_every_star_its_own_way_and_the_sky_stays_bare_a_minute() -> None:
    scene, left_at, path = _feeding(1)
    assert left_at < StarWhale.max_feeding and all(s.eaten_at is not None for s in scene._stars)
    assert all(s.back_at - s.eaten_at >= 60 for s in scene._stars if s.eaten_at is not None)
    assert _feeding(2)[2] != path  # same sky, another whale: another hunt
