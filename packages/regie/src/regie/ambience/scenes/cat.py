"""A fat orange pixel cat under the tree: it sits, eats, drinks, and hops off to nap on its bed.

This module is its life; ``cat_art`` draws it. Positions are pixels: ``x`` is the cat's centre
column and ``floor`` the pixel row under its feet (two pixel rows per band row).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from regie.ambience.pixels import Canvas, to_cells
from regie.ambience.scene import Cell, Phase, Scene
from regie.ambience.scenes import cat_art as art

_WIDE, _TALL = 28, 26  # columns and pixel rows it needs at full size
_SMALLEST = 0.8  # below this it loses its face, so a smaller band shows no cat


@dataclass(frozen=True, slots=True)
class _Hop:
    x0: float
    y0: float
    x1: float
    y1: float
    lift: float  # pixels above the straight line at the top of the arc
    flight: float  # seconds in the air

    def at(self, done: float) -> tuple[int, int]:
        x = self.x0 + (self.x1 - self.x0) * done
        y = self.y0 + (self.y1 - self.y0) * done - self.lift * 4 * done * (1 - done)
        return round(x), round(y)


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
        self.x = self.floor = 0
        self.food = self.water = self.bed = (0, 0)
        self.has_bed = False
        self.state, self.left, self.plan = "sit", 3.0, "sit"
        self.target = (0, 0)
        self.hop: _Hop | None = None
        self.zs: list[_Z] = []
        self._placed = False

    @property
    def _fits(self) -> bool:
        return self.s >= _SMALLEST and self.width >= _WIDE * self.s

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.s = min(1.0, height * 2 / _TALL)
        if not self._fits:
            return
        inside = self._spot_ok
        if not self._placed or not all(inside(*spot) for spot in (self.food, self.water)):
            self.food = self._spot()
            self.water = self._spot(self.food)
        if not inside(self.x, self.floor) or not self._placed:
            self.x, self.floor = self._spot()
        self.has_bed = self.has_bed and inside(*self.bed)
        self.hop, self.state, self.left, self._placed = None, "sit", 2.0, True

    def _spot(self, *taken: tuple[int, int]) -> tuple[int, int]:
        """A place for its feet: clear of ``taken`` ones if the band allows, else not on them."""
        half, tall = math.ceil(_WIDE * self.s / 2), math.ceil((_TALL - 2) * self.s)
        for tries in range(80):
            x = self.rng.randrange(half, self.width - half + 1)
            floor = self.rng.randrange(tall, self.height * 2)
            gap, rows = (2 * half, tall) if tries < 40 else (13, 5)  # a small band: bowls apart
            if all(abs(x - tx) >= gap or abs(floor - ty) >= rows for tx, ty in taken):
                break
        return x, floor

    def _spot_ok(self, x: int, floor: int) -> bool:
        half, tall = math.ceil(_WIDE * self.s / 2), math.ceil((_TALL - 2) * self.s)
        return half <= x <= self.width - half and tall <= floor < self.height * 2

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if not self._fits or (phase is Phase.OUTRO and progress >= 1):
            return []
        self.t += dt
        if phase is Phase.IDLE:
            self._live(dt)
        self._drift_zs(dt)
        canvas: Canvas = {}
        if phase is not Phase.OUTRO or progress < 0.6:
            self._furniture(canvas)
        if phase is Phase.IDLE:
            self._cat(canvas)
        else:  # it drops in from above, and leaps away up out of the band
            fall = 1 - progress if phase is Phase.INTRO else progress
            art.leap(canvas, self.x, self.floor - fall * (self.floor + 8), self.s, 1)
        snores = [Cell(round(z.x), round(z.y), "zZz"[int(z.age)], "$text-muted") for z in self.zs]
        return to_cells(canvas) + snores

    def _furniture(self, canvas: Canvas) -> None:
        ripple = art.RIPPLE if int(self.t * 4) % 2 else art.WATER
        for (x, floor), fill in ((self.food, art.KIBBLE), (self.water, ripple)):
            art.dish(canvas, x, floor, self.s, fill)
        if self.has_bed and self.state != "sleep":
            share = 1.0 if self.state != "bed" else 1 - max(0.0, self.left) / 0.6
            art.bed(canvas, self.bed[0], self.bed[1], self.s, share)

    def _live(self, dt: float) -> None:
        self.left -= dt
        if self.state == "hop" and self.hop:
            if self.left <= 0:
                self.x, self.floor = self.hop.at(1.0)
                self.hop = None
                self._arrive()
            return
        if self.state == "sleep" and int(self.t / 1.3) != int((self.t - dt) / 1.3):
            self.zs.append(_Z(self.x - 9 * self.s, (self.floor - 16 * self.s) / 2))
        if self.left > 0:
            return
        if self.state == "wiggle":
            self._leap()
        elif self.state == "bed":
            self.state, self.left = "sleep", self.rng.uniform(20.0, 40.0)
        else:
            self._choose()

    def _choose(self) -> None:
        here = (self.x, self.floor)
        if self.state == "eat":  # a finished bowl is put down somewhere else
            self.food = self._spot(self.water, here)
        elif self.state == "drink":
            self.water = self._spot(self.food, here)
        plans = [p for p in ("eat", "drink", "sleep", "sit") if p != self.plan]
        self.plan = self.rng.choices(plans, [1 if p == "sit" else 3 for p in plans])[0]
        bowls = {"eat": self.food, "drink": self.water}
        target = bowls.get(self.plan) or self._spot(self.food, self.water)
        if target == here:
            self._arrive()
        else:
            self.target, self.state, self.left = target, "wiggle", 0.6

    def _leap(self) -> None:
        (x1, y1), x0, y0 = self.target, float(self.x), float(self.floor)
        span = math.hypot(x1 - x0, (y1 - y0) / 2)
        self.hop = _Hop(x0, y0, x1, y1, 5 + span / 4, 0.7 + span / 30)
        self.state, self.left = "hop", self.hop.flight

    def _arrive(self) -> None:
        if self.plan == "sleep":
            self.bed, self.has_bed = (self.x, self.floor), True
            self.state, self.left = "bed", 0.6
        elif self.plan in ("eat", "drink"):
            self.state, self.left = self.plan, self.rng.uniform(4.0, 8.0)
        else:
            self.state, self.left = "sit", self.rng.uniform(3.0, 8.0)

    def _drift_zs(self, dt: float) -> None:
        for z in self.zs:
            z.age += dt
            z.x, z.y = z.x - 0.8 * dt, z.y - 0.7 * dt
        self.zs = [z for z in self.zs if z.age < 3.0 and z.y >= 0]

    def _cat(self, canvas: Canvas) -> None:
        x, floor, s, t = self.x, self.floor, self.s, self.t
        if self.state == "hop" and self.hop:
            hx, feet = self.hop.at(1 - max(0.0, self.left) / self.hop.flight)
            art.leap(canvas, hx, feet, s, 1 if self.hop.x1 >= self.hop.x0 else -1)
        elif self.state == "sleep":
            art.sleep(canvas, x, floor, s, t)
        elif self.state in ("eat", "drink"):
            art.graze(canvas, x, floor, s, t, drinking=self.state == "drink")
        else:
            wiggle = self.state == "wiggle" and int(t * 8) % 2
            art.sit(canvas, x + wiggle, floor, s, t, blink=int(t * 8) % 37 == 0)


__all__ = ["CatScene"]
