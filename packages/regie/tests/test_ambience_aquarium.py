from __future__ import annotations

from regie.ambience.driver import AmbienceDriver
from regie.ambience.scene import Cell, Phase
from regie.ambience.scenes.aquarium import AquariumScene


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
        first_bubble = next((cell for cell in cells if cell.glyph in "°o○·"), None)
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
