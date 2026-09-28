"""The tree ambience framework: phase machine, scene contract, and the band under the tree."""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest
from regie.ambience.driver import AmbienceDriver
from regie.ambience.pixels import HALF_BLOCKS
from regie.ambience.registry import load_catalog
from regie.ambience.render import render_band
from regie.ambience.scene import MAX_TRANSITION_SECONDS, Cell, Phase, Scene
from regie.widgets.ambience_band import AmbienceBand
from textual import events

from packages.regie.tests.test_ui import _app
from tests.rig.waiting import wait_until

SHIPPED = load_catalog(Path("/nonexistent")).scenes


class Probe(Scene):
    name = "probe"
    fps = 20.0
    intro_seconds = 1.0
    outro_seconds = 1.0

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if phase is Phase.OUTRO and progress >= 1.0:
            return []
        return [Cell(0, self.height - 1, phase.value[0]), Cell(99, 99, "clipped")]


def _driver(width: int = 20, height: int = 5, *, active: bool = True) -> AmbienceDriver:
    driver = AmbienceDriver(Probe, seed=1)
    driver.set_band(width, height)
    driver.set_active(active)
    return driver


def test_focus_plays_intro_then_idle_and_blur_plays_an_outro_to_nothing() -> None:
    driver = _driver()
    assert driver.phase is Phase.INTRO
    assert driver.tick(0.5) == [Cell(0, 4, "i")]  # out-of-band cells are clipped
    driver.tick(0.6)
    assert driver.phase is Phase.IDLE
    driver.set_active(False)
    assert driver.phase is Phase.OUTRO
    driver.tick(0.5)
    assert driver.tick(0.6) == [] and not driver.running


def test_a_transition_in_flight_reverses_from_where_it_is() -> None:
    driver = _driver()
    driver.tick(0.25)  # intro a quarter done
    driver.set_active(False)
    driver.tick(0.2)  # the outro began three quarters done: nearly over
    assert driver.running
    driver.set_active(True)
    assert driver.phase is Phase.INTRO
    driver.tick(0.9)  # the outro had almost cleared the band: the intro replays nearly all
    assert driver.phase is Phase.INTRO
    driver.tick(0.1)
    assert driver.phase is Phase.IDLE


def test_no_room_stops_everything_at_once_and_room_restarts_it() -> None:
    driver = _driver()
    driver.set_band(20, 1)  # below the scene's two-row minimum
    assert not driver.running and driver.tick(0.1) == []
    driver.set_band(20, 3)
    assert driver.phase is Phase.INTRO
    idle = _driver(active=False)
    idle.set_band(20, 6)
    assert not idle.running  # room alone never starts it: focus does


def test_wide_glyphs_take_two_cells_and_never_overflow_the_band() -> None:
    text = render_band(
        [Cell(0, 0, "🐟", "$accent"), Cell(2, 0, "🔥"), Cell(3, 1, "🔥"), Cell(1, 1, "x")],
        width=4,
        height=2,
    ).plain
    assert text.splitlines() == ["🐟🔥", " x  "]  # the last-column fire would overflow


@pytest.mark.parametrize("scene", list(SHIPPED.values()), ids=list(SHIPPED))
@pytest.mark.parametrize(("width", "height"), [(40, 12), (30, 3), (12, 2), (40, 1)])
def test_every_scene_honours_the_contract(scene: type[Scene], width: int, height: int) -> None:
    """Short transitions, in-band ASCII cells at any size, an empty end, and determinism."""
    assert scene.intro_seconds <= MAX_TRANSITION_SECONDS
    assert scene.outro_seconds <= MAX_TRANSITION_SECONDS
    assert 1 <= scene.min_rows <= 2 and 0 < scene.fps <= 10

    def run(seed: int) -> list[list[Cell]]:
        driver = AmbienceDriver(scene, seed=seed)
        driver.set_band(width, height)
        driver.set_active(True)
        frames = [driver.tick(1 / scene.fps) for _ in range(int(scene.fps * 20))]
        driver.set_active(False)
        frames += [driver.tick(1 / scene.fps) for _ in range(int(scene.fps * 6))]
        assert not driver.running  # the outro finished within its bound
        return frames

    frames = run(7)
    assert frames == run(7)
    for cell in (cell for frame in frames for cell in frame):
        assert 0 <= cell.x < width and 0 <= cell.y < height
        ascii_art = cell.glyph.isascii() and cell.glyph.isprintable()
        assert ascii_art or cell.glyph in HALF_BLOCKS  # no other glyphs, no emoji
    if height < scene.min_rows:
        assert not any(frames)


def test_a_scene_sees_a_seeded_rng() -> None:
    assert Probe(random.Random(3)).rng.random() == random.Random(3).random()


class _Sticky(Probe):
    """Draws in every phase so the band's visibility shows the driver's state."""

    min_rows: ClassVar[int] = 2


async def _set_app_focus(pilot, focused: bool) -> None:
    """Deliver focus as the terminal does, in order with Textual's own startup focus."""
    pilot.app.post_message(events.AppFocus() if focused else events.AppBlur())
    await wait_until(pilot, lambda: pilot.app.app_focus is focused)


@pytest.mark.parametrize("when", ["away", "tree"])
async def test_the_band_plays_under_the_tree_when_its_setting_says(
    monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """The default, away, plays while a staged surface has focus; tree plays on the tree."""
    monkeypatch.setattr("regie.app_parts.ambience.scene_for", lambda _name: _Sticky)
    app, _client, _presentation = _app()
    app.settings = replace(app.settings, tree_ambience_when=when)
    on_tree = when == "tree"
    async with app.run_test(size=(80, 30)) as pilot:
        band = app.query_one(AmbienceBand)
        await wait_until(pilot, lambda: app._ambience is not None)
        await _set_app_focus(pilot, on_tree)  # start where the band plays
        await wait_until(pilot, lambda: bool(band.display))
        tree_bottom = max(leaf.region.bottom for leaf in app.query("AgentLeaf"))
        assert band.region.y >= tree_bottom  # below the last row, never over it

        await _set_app_focus(pilot, not on_tree)  # to or from a staged terminal
        await wait_until(pilot, lambda: not band.display)
        await _set_app_focus(pilot, True)
        await pilot.press("h", "h")  # open the trajectory and focus it
        await wait_until(pilot, app._trajectory_has_focus)
        await wait_until(pilot, lambda: bool(band.display) is not on_tree)
        await pilot.press("escape")
        await wait_until(pilot, lambda: bool(band.display) is on_tree)


async def test_a_tree_without_free_rows_plays_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("regie.app_parts.ambience.scene_for", lambda _name: _Sticky)
    app, _client, _presentation = _app()
    async with app.run_test(size=(80, 7)) as pilot:  # two leaves already overflow the tree
        await wait_until(pilot, lambda: app._ambience is not None)
        await _set_app_focus(pilot, False)  # away: the band would play if it fit
        await pilot.pause(0.3)
        assert not app.query_one(AmbienceBand).display


def test_tree_ambience_is_validated_and_defaults_to_the_footer_while_away(tmp_path) -> None:
    from regie.config import SettingsError, load_settings

    assert load_settings(tmp_path / "missing.toml").tree_ambience_when == "away"
    when = tmp_path / "when.toml"
    when.write_text('[regie]\ntree_ambience_when = "sometimes"\n')
    with pytest.raises(SettingsError, match="tree_ambience_when"):
        load_settings(when)

    good, bad = tmp_path / "good.toml", tmp_path / "bad.toml"
    good.write_text('[regie]\ntree_ambience = "none"\n')
    bad.write_text('[regie]\ntree_ambience = "lava"\n')
    assert load_settings(tmp_path / "missing.toml").tree_ambience == "footer"
    assert load_settings(good).tree_ambience == "none"
    with pytest.raises(SettingsError, match="tree_ambience"):
        load_settings(bad)
