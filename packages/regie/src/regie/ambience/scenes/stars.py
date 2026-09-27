"""A small starry sky under the tree: twinkles, a rare shooting star, rarer visitors.

Stars sit at stable positions; only their glyphs breathe. On resize the
layout re-seeds: kept stars stay, the rest of the sky is re-rolled.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from regie.ambience.scene import Cell, Phase, Scene

#: Weighted: mostly faint dots, the odd bright one.
_POOL = (".", ".", ".", ".", "'", "'", "+", "*")
_STYLE = {".": "$text-muted", "'": "$secondary dim", "+": "$text", "*": "$text"}
_HEAD, _TAIL, _TRAIL = ("*", "$accent"), ("-", "$secondary dim"), (".", "$text-muted")


@dataclass(frozen=True, slots=True)
class _Sprite:
    rows: tuple[str, ...]
    speed: float  # columns per second
    rightward: bool
    style: str

    @property
    def width(self) -> int:
        return max(len(row) for row in self.rows)


_SHIP = _Sprite(("<o=-",), 3.0, False, "$warning")
_WHALE = _Sprite(
    (
        r"          .-'''''-.",
        r"\\      .'  *   .  `.",
        r" >====(  .   *    o )",
        r"//      `-._______.-'",
    ),
    2.0,
    True,
    "$primary",
)


@dataclass(slots=True)
class _Star:
    x: int
    y: int
    glyph: str

    def cell(self) -> Cell:
        return Cell(self.x, self.y, self.glyph, _STYLE[self.glyph])


class StarsScene(Scene):
    name = "stars"
    fps = 4.0
    min_rows = 2
    intro_seconds = 2.5
    outro_seconds = 2.0

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self._stars: list[_Star] = []
        self._order: list[int] = []
        self._shoot: tuple[float, float] | None = None
        self._next_shoot = 20.0 + rng.random() * 20.0
        self._visitor: tuple[_Sprite, float, int] | None = None  # sprite, left x, top y
        self._next_visit = 45.0 + rng.random() * 45.0

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
        if self._visitor and self._visitor[2] + len(self._visitor[0].rows) > height:
            self._visitor = None

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if phase is Phase.OUTRO:
            self._shoot = self._visitor = None
            if progress >= 1.0:
                return []
        if self.width <= 0 or self.height < self.min_rows:
            return []
        total = len(self._stars)
        if phase is Phase.INTRO:
            visible = math.ceil(total * progress)
        elif phase is Phase.OUTRO:
            visible = math.ceil(total * (1.0 - progress))
        else:
            visible = total
            self._twinkle()
            self._shoot_star(progress, dt)
            self._visit(progress, dt)
        hidden = self._hull()
        cells = [self._stars[i].cell() for i in self._order[:visible]]
        cells = [cell for cell in cells if (cell.x, cell.y) not in hidden]
        cells.extend(self._shoot_cells())
        cells.extend(self._visitor_cells())
        return cells

    def _twinkle(self) -> None:
        for star in self._stars:
            if self.rng.random() < 0.02:  # a few stars at a time, so the sky stays calm
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
                whale = self.rng.random() < 0.3 and len(_WHALE.rows) <= self.height
                sprite = _WHALE if whale else _SHIP
                start = -sprite.width if sprite.rightward else self.width
                y = self.rng.randrange(self.height - len(sprite.rows) + 1)
                self._visitor = (sprite, float(start), y)
                self._next_visit = idle + 60.0 + self.rng.random() * 60.0
            return
        sprite, x, y = self._visitor
        x += sprite.speed * dt if sprite.rightward else -sprite.speed * dt
        self._visitor = (sprite, x, y) if -sprite.width <= x <= self.width else None

    def _hull(self) -> set[tuple[int, int]]:
        """The visitor's outline, row by row, so stars behind its body stay hidden."""
        if self._visitor is None:
            return set()
        sprite, x, y = self._visitor
        seats: set[tuple[int, int]] = set()
        for dy, row in enumerate(sprite.rows):
            start = len(row) - len(row.lstrip())
            seats.update((int(x) + dx, y + dy) for dx in range(start, len(row)))
        return seats

    def _visitor_cells(self) -> list[Cell]:
        if self._visitor is None:
            return []
        sprite, x, y = self._visitor
        return [
            Cell(int(x) + dx, y + dy, glyph, "$text" if glyph == "*" else sprite.style)
            for dy, row in enumerate(sprite.rows)
            for dx, glyph in enumerate(row)
            if glyph != " "
        ]

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
