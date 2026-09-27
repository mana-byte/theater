"""A fat orange pixel cat sitting centred at the bottom of the band: it eats, drinks and naps there.

This module is its life; ``cat_art`` draws it. Positions are pixels, two pixel rows per band row;
the cat's centre column is the band's middle, and it rests on the band's bottom edge.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from regie.ambience.pixels import Canvas, to_cells
from regie.ambience.scene import Cell, Phase, Scene
from regie.ambience.scenes import cat_art as art

_WIDE, _TALL = 28, 26  # columns and pixel rows it needs at full size
_SMALLEST = 0.8  # below this it loses its face, so a smaller band shows no cat
_BED_SECONDS = 0.6  # the bed popping in under it before it curls up
#: How long each activity lasts, in seconds.
_LASTS = {"sit": (3.0, 8.0), "eat": (4.0, 8.0), "drink": (4.0, 8.0), "sleep": (20.0, 40.0)}


@dataclass(slots=True)
class _Z:
    x: float
    y: float
    age: float = 0.0


class CatScene(Scene):
    name = "cat"
    fps = 8.0
    min_rows = 2
    intro_seconds = 2.0
    outro_seconds = 1.5

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self.t, self.s = 0.0, 1.0
        self.state, self.left = "sit", 3.0
        self.zs: list[_Z] = []

    @property
    def _fits(self) -> bool:
        return self.s >= _SMALLEST and self.width >= _WIDE * self.s

    @property
    def x(self) -> int:
        return self.width // 2

    @property
    def floor(self) -> int:
        return self.height * 2 - 2  # its outline takes the band's last pixel row

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.s = min(1.0, height * 2 / _TALL)

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if not self._fits or (phase is Phase.OUTRO and progress >= 1):
            return []
        self.t += dt
        if phase is Phase.IDLE:
            self._live(dt)
        self._drift_zs(dt)
        canvas: Canvas = {}
        if phase is Phase.IDLE:
            self._cat(canvas)
        else:  # it drops in from above, and leaps away up out of the band
            fall = 1 - progress if phase is Phase.INTRO else progress
            art.leap(canvas, self.x, self.floor - fall * (self.floor + 8), self.s, 1)
        snores = [Cell(round(z.x), round(z.y), "zZz"[int(z.age)], "$text-muted") for z in self.zs]
        return to_cells(canvas) + snores

    def _live(self, dt: float) -> None:
        self.left -= dt
        if self.state == "sleep" and int(self.t / 1.3) != int((self.t - dt) / 1.3):
            self.zs.append(_Z(self.x - 9 * self.s, (self.floor - 16 * self.s) / 2))
        if self.left > 0:
            return
        if self.state == "bed":
            self._begin("sleep")
        else:
            nexts = [state for state in _LASTS if state != self.state]
            chosen = self.rng.choices(nexts, [1 if s == "sit" else 3 for s in nexts])[0]
            if chosen == "sleep":
                self.state, self.left = "bed", _BED_SECONDS
            else:
                self._begin(chosen)

    def _begin(self, state: str) -> None:
        self.state, self.left = state, self.rng.uniform(*_LASTS[state])

    def _drift_zs(self, dt: float) -> None:
        for z in self.zs:
            z.age += dt
            z.x, z.y = z.x - 0.8 * dt, z.y - 0.7 * dt
        self.zs = [z for z in self.zs if z.age < 3.0 and z.y >= 0]

    def _cat(self, canvas: Canvas) -> None:
        x, floor, s, t = self.x, self.floor, self.s, self.t
        if self.state == "sleep":
            art.sleep(canvas, x, floor, s, t)
        elif self.state in ("eat", "drink"):
            art.graze(canvas, x, floor, s, t, drinking=self.state == "drink")
        else:
            if self.state == "bed":
                art.bed(canvas, x, floor, s, 1 - max(0.0, self.left) / _BED_SECONDS)
            art.sit(canvas, x, floor, s, t, blink=int(t * 8) % 37 == 0)


__all__ = ["CatScene"]
