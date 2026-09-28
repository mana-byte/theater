"""A small starry sky under the tree: twinkles, a rare shooting star, rarer visitors.

Stars sit at stable positions; only their glyphs breathe. On resize the
layout re-seeds: kept stars stay, the rest of the sky is re-rolled.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from regie.ambience.api import Cell, Phase, Scene

from .visitors import Ship, Visitor
from .whale import StarWhale

#: Weighted: mostly faint dots, the odd bright one.
_POOL = (".", ".", ".", ".", "'", "'", "+", "*")
_STYLE = {".": "$text-muted", "'": "$secondary dim", "+": "$text", "*": "$text"}
_HEAD, _TAIL, _TRAIL = ("*", "$accent"), ("-", "$secondary dim"), (".", "$text-muted")
#: An eaten star stays gone while the whale feeds and at least a minute, then grows back.
_REGROW_SECONDS = (60.0, 90.0)


@dataclass(slots=True)
class _Star:
    x: int
    y: int
    glyph: str
    eaten_at: float | None = None
    back_at: float = 0.0  # the scene clock at which an eaten star shines again

    def cell(self) -> Cell:
        return Cell(self.x, self.y, self.glyph, _STYLE[self.glyph])


class StarsScene(Scene):
    name = "stars"
    fps = 8.0  # smooth enough for the whale's glide
    min_rows = 2
    intro_seconds = 2.5
    outro_seconds = 2.0

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self._stars: list[_Star] = []
        self._order: list[int] = []
        self._shoot: tuple[float, float] | None = None
        self._next_shoot = 20.0 + rng.random() * 20.0
        self._visitor: Visitor | None = None
        self._next_visit = 45.0 + rng.random() * 45.0
        self._clock = 0.0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self._stars = [s for s in self._stars if s.x < width and s.y < height]
        taken = {(s.x, s.y) for s in self._stars}
        target = min(max(2, width * height // 24), width * height)
        while len(self._stars) < target and len(taken) < width * height:
            x, y = self.rng.randrange(width), self.rng.randrange(height)
            if (x, y) in taken:
                continue
            taken.add((x, y))
            self._stars.append(_Star(x, y, self.rng.choice(_POOL)))
        self._order = list(range(len(self._stars)))
        self.rng.shuffle(self._order)
        if self._visitor and not self._visitor.resize(width, height):
            self._visitor = None

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if phase is Phase.OUTRO:
            self._shoot = self._visitor = None
            if progress >= 1.0:
                return []
        if self.width <= 0 or self.height < self.min_rows:
            return []
        self._clock += dt
        total = len(self._stars)
        if phase is Phase.INTRO:
            visible = math.ceil(total * progress)
        elif phase is Phase.OUTRO:
            visible = math.ceil(total * (1.0 - progress))
        else:
            visible = total
            self._twinkle(dt)
            self._shoot_star(progress, dt)
            self._visit(progress, dt)
        hidden = self._visitor.hull() if self._visitor else set()
        shining = [self._stars[i] for i in self._order[:visible]]
        cells = [star.cell() for star in shining if star.back_at <= self._clock]
        cells = [cell for cell in cells if (cell.x, cell.y) not in hidden]
        cells.extend(self._shoot_cells())
        cells.extend(self._visitor.cells() if self._visitor else ())
        return cells

    def _twinkle(self, dt: float) -> None:
        for star in self._stars:
            if self.rng.random() < 0.08 * dt:  # a few stars at a time, so the sky stays calm
                star.glyph = self.rng.choice(_POOL)

    def _shoot_star(self, idle: float, dt: float) -> None:
        if self._shoot is None:
            if idle >= self._next_shoot and self.width > 4:
                self._shoot = (-1.0, self.rng.random() * max(1, self.height // 2))
                self._next_shoot = idle + 20.0 + self.rng.random() * 20.0
            return
        x, y = self._shoot[0] + 8.0 * dt, self._shoot[1] + 2.0 * dt
        self._shoot = (x, y) if x < self.width and y < self.height else None

    def _visit(self, idle: float, dt: float) -> None:
        if self._visitor is None:
            if idle >= self._next_visit:
                whale = self.rng.random() < 0.3 and self.height >= StarWhale.rows_needed
                kind = StarWhale if whale else Ship
                self._visitor = kind(self.rng, self.width, self.height)
            return
        sky = {(s.x, s.y): s for s in self._stars if s.back_at <= self._clock}
        for seat in self._visitor.step(dt, frozenset(sky)):
            sky[seat].eaten_at, sky[seat].back_at, sky[seat].glyph = self._clock, math.inf, "."
        if self._visitor.gone:
            self._visitor, self._next_visit = None, idle + 60.0 + self.rng.random() * 60.0
            for star in self._stars:
                if star.eaten_at is not None and star.back_at == math.inf:
                    star.back_at = star.eaten_at + self.rng.uniform(*_REGROW_SECONDS)
                    star.back_at = max(star.back_at, self._clock + self.rng.uniform(5.0, 30.0))

    def _shoot_cells(self) -> list[Cell]:
        if self._shoot is None:
            return []
        x, y = int(self._shoot[0]), int(self._shoot[1])
        cells = [Cell(x, y, *_HEAD)]
        if x >= 2 and y >= 1:
            cells.append(Cell(x - 1, y, *_TAIL))
            cells.append(Cell(x - 2, y - 1, *_TRAIL))
        return cells


__all__ = ["StarsScene"]
