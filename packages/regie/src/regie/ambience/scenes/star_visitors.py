"""Rare visitors crossing the stars: a small ship, or the star whale — the sky's super event."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from regie.ambience.scene import Cell

#: Tail flukes mid, up, mid, down; ``*`` marks the whale's shimmering, starry insides.
_MID = (
    r"          .-'''''-.",
    r"\\      .'  *   *  `.",
    r" >====(  *   *    o )",
    r"//      `-._______.-'",
)
_UP = (
    r"\\        .-'''''-.",
    r" \\     .'  *   *  `.",
    r"  >===(  *   *    o )",
    r"        `-._______.-'",
)
_DOWN = (
    r"          .-'''''-.",
    r"        .'  *   *  `.",
    r"  >===(  *   *    o )",
    r" //     `-._______.-'",
)
_POSES = (_MID, _UP, _MID, _DOWN)
_SHIMMER = "*+.'"
_BLOWHOLE = 14


def _hull(rows: tuple[str, ...], left: int, top: int) -> set[tuple[int, int]]:
    """Each row from its first to last glyph, so stars behind the body stay hidden."""
    seats: set[tuple[int, int]] = set()
    for dy, row in enumerate(rows):
        start = len(row) - len(row.lstrip())
        seats.update((left + dx, top + dy) for dx in range(start, len(row)))
    return seats


class Visitor:
    """Something drifting across the band once; ``step`` returns False when it has gone."""

    def step(self, dt: float) -> bool:
        raise NotImplementedError

    def cells(self) -> list[Cell]:
        raise NotImplementedError

    def hull(self) -> set[tuple[int, int]]:
        raise NotImplementedError

    def resize(self, width: int, height: int) -> bool:
        """The band changed; False drops the visitor, which no longer fits."""
        raise NotImplementedError


class Ship(Visitor):
    body, style, speed = "<o=-", "$warning", 3.0

    def __init__(self, rng: random.Random, width: int, height: int) -> None:
        self.x, self.y = float(width), rng.randrange(height)

    def step(self, dt: float) -> bool:
        self.x -= self.speed * dt
        return self.x > -len(self.body)

    def cells(self) -> list[Cell]:
        return [Cell(int(self.x) + i, self.y, g, self.style) for i, g in enumerate(self.body)]

    def hull(self) -> set[tuple[int, int]]:
        return _hull((self.body,), int(self.x), self.y)

    def resize(self, width: int, height: int) -> bool:
        return self.y < height


@dataclass(slots=True)
class _Mote:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    style: str
    age: float = 0.0

    def glyph(self) -> str:
        share = self.age / self.life
        return "*" if share < 0.35 else "+" if share < 0.7 else "."


class StarWhale(Visitor):
    """Glides slowly on a swell, flukes beating, leaving a glittering wake, spouting stardust."""

    rows_needed, style, speed = len(_MID), "$primary", 1.5
    wave_seconds, spout_every = 18.0, 5.0

    def __init__(self, rng: random.Random, width: int, height: int) -> None:
        self.rng, self.width, self.height = rng, width, height
        self.x, self.t = -float(len(_MID[1])), 0.0
        self.swell = rng.random() * math.tau
        self.motes: list[_Mote] = []
        self._wake_due = self._spout_due = 0.0

    @property
    def top(self) -> int:
        mid = (self.height - len(_MID)) / 2
        return round(mid + mid * math.sin(self.swell + self.t * math.tau / self.wave_seconds))

    def step(self, dt: float) -> bool:
        self.t += dt
        self.x += self.speed * dt
        for mote in self.motes:
            mote.age += dt
            mote.x, mote.y = mote.x + mote.vx * dt, mote.y + mote.vy * dt
        self.motes = [m for m in self.motes if m.age < m.life]
        if self.x < self.width:
            self._shed(dt)
        return self.x < self.width or bool(self.motes)

    def _shed(self, dt: float) -> None:
        self._wake_due -= dt
        if self._wake_due <= 0:
            self._wake_due = 0.25
            jitter = self.rng.uniform(-0.6, 0.6)
            self.motes.append(_Mote(self.x, self.top + 2 + jitter, 0.0, 0.0, 3.0, "$secondary"))
        self._spout_due -= dt
        if self._spout_due <= 0 and self.top >= 1:
            self._spout_due = self.spout_every
            for vx in (-1.5, -0.5, 0.5, 1.5):
                x = self.x + _BLOWHOLE
                self.motes.append(_Mote(x, self.top - 0.5, vx, -2.0, 1.2, "$text"))

    def cells(self) -> list[Cell]:
        pose = _POSES[int(self.t * 2) % len(_POSES)]
        left, top, beat = int(self.x), self.top, int(self.t * 3)
        motes = [Cell(int(m.x), round(m.y), m.glyph(), m.style) for m in self.motes]
        body = [
            Cell(left + dx, top + dy, _SHIMMER[(dx * 7 + dy * 3 + beat) % 4], "$text")
            if glyph == "*"
            else Cell(left + dx, top + dy, glyph, self.style)
            for dy, row in enumerate(pose)
            for dx, glyph in enumerate(row)
            if glyph != " "
        ]
        return motes + body if self.x < self.width else motes

    def hull(self) -> set[tuple[int, int]]:
        return _hull(_MID, int(self.x), self.top) if self.x < self.width else set()

    def resize(self, width: int, height: int) -> bool:
        self.width, self.height = width, height
        return height >= self.rows_needed


__all__ = ["Ship", "StarWhale", "Visitor"]
