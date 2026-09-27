"""A fat orange cat under the tree: it sits, eats, drinks, and hops off to make its bed and nap.

Poses are hand-drawn; capital letters are parts painted per frame (eyes, jaw, bowl), so one
template serves every blink, bite and lap. The whole scene is drawn from its feet up.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from regie.ambience.scene import Cell, Phase, Scene

_FUR, _EYE, _PINK = "#f5a142", "#7bd88f", "#ff8fab"
_BED, _RIM, _FOOD, _WATER = "#8e7cc3", "#9aa5b1", "#b5651d", "#5dade2"

#: Sitting, breathing in and out: the tummy swells and the whole body bounces with it.
_SIT = (
    (
        r"    /\_/\    ",
        r"   ( E.E )   ",
        r"  .-> N <-.  ",
        r" (  (   )  ) ",
        r"  `-(_)(_)-' ",
    ),
    (
        r"    /\_/\    ",
        r"   ( E.E )   ",
        r" .--> N <--. ",
        r"(  (     )  )",
        r" `--(_)(_)--'",
    ),
)
#: Head down in the bowl: ``J`` is the muzzle's jaw, ``LFR`` the bowl and what is in it.
_BOWL = (
    (
        r"             ",
        r"    /\_/\    ",
        r" .-( C.C )-. ",
        r"(    \J/    )",
        r" `-(LFFFR)-' ",
    ),
    (
        r"             ",
        r"    /\_/\    ",
        r".--( C.C )--.",
        r"(    \J/    )",
        r" `-(LFFFR)-' ",
    ),
)
#: Curled up on its bed, the back rising and falling with each breath.
_SLEEP = (
    (
        r"        .-'''-.   ",
        r"  /\_/\'       `. ",
        r" ( C.C )         )",
        r" (__(__)______,-'~",
    ),
    (
        r"                  ",
        r"  /\_/\.-'''''-.  ",
        r" ( C.C )        `.",
        r" (__(__)_________)~",
    ),
)
_JUMP = (
    r"    _____/\_/\  ",
    r"~~ (      E.E ) ",
    r"   / /-----\ \  ",
)
_TAILS = (("   ", "  )", "_/ "), ("  )", "  |", "_/ "), ("   ", " (_", "_/ "))
_BED_ROW = "(==================)"
_MIRROR = str.maketrans("/\\<>()`'", "\\/><)('`")
_WIDE = 24  # the cat needs this many columns, and five rows, to be drawn at all
_ROWS = 5


@dataclass(frozen=True, slots=True)
class _Hop:
    x0: float
    y0: float
    x1: float
    y1: float
    lift: float  # rows above the straight line at the top of the arc
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
        self.t = 0.0
        self.x = self.floor = 0  # where its feet are: left column, bottom row
        self.food = self.water = self.bed = (0, 0)
        self.has_bed = False
        self.state, self.left, self.plan = "sit", 3.0, "sit"
        self.target = (0, 0)
        self.hop: _Hop | None = None
        self.zs: list[_Z] = []
        self._placed = False

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        if width < _WIDE or height < _ROWS:
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
        for tries in range(80):
            x, floor = (
                self.rng.randrange(self.width - _WIDE + 1),
                self.rng.randrange(4, self.height),
            )
            gap, rows = (_WIDE, _ROWS) if tries < 40 else (7, 1)  # a small band: bowls apart
            if all(abs(x - tx) >= gap or abs(floor - ty) >= rows for tx, ty in taken):
                break
        return x, floor

    def _spot_ok(self, x: int, floor: int) -> bool:
        return 0 <= x <= self.width - _WIDE and 4 <= floor < self.height

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if self.width < _WIDE or self.height < _ROWS or (phase is Phase.OUTRO and progress >= 1):
            return []
        self.t += dt
        if phase is Phase.IDLE:
            self._live(dt)
        self._drift_zs(dt)
        cells = self._furniture(phase, progress)
        if phase is Phase.IDLE:
            cells += self._cat()
        else:  # it drops in from above, and leaps away up out of the band
            fall = 1 - progress if phase is Phase.INTRO else progress
            cells += self._sprite(_JUMP, self.x, round(self.floor - fall * (self.floor + 3)), 1)
        return cells + [
            Cell(round(z.x), round(z.y), "zZz"[min(2, int(z.age))], "$text-muted") for z in self.zs
        ]

    def _furniture(self, phase: Phase, progress: float) -> list[Cell]:
        if phase is Phase.OUTRO and progress > 0.6:
            return []
        cells = self._bowl(self.food, _FOOD, ":") + self._bowl(self.water, _WATER, "~")
        if self.has_bed:
            grown = (
                len(_BED_ROW) if self.state != "bed" else int(len(_BED_ROW) * (1 - self.left / 0.6))
            )
            x, y = self.bed
            start = x + (len(_BED_ROW) - grown) // 2
            cells += [Cell(start + i, y, g, _BED) for i, g in enumerate(_BED_ROW[:grown])]
        return cells

    def _bowl(self, spot: tuple[int, int], style: str, fill: str) -> list[Cell]:
        x, y = spot[0] + 4, spot[1]
        ripple = fill == "~" and int(self.t * 4) % 2
        return [Cell(x, y, "\\", _RIM), Cell(x + 4, y, "/", _RIM)] + [
            Cell(x + i, y, "-" if ripple and i == 2 else fill, style) for i in (1, 2, 3)
        ]

    def _live(self, dt: float) -> None:
        self.left -= dt
        if self.state == "hop" and self.hop:
            if self.left <= 0:
                self.x, self.floor = self.hop.at(1.0)
                self.hop = None
                self._arrive()
            return
        if self.state == "sleep" and int(self.t / 1.3) != int((self.t - dt) / 1.3):
            self.zs.append(_Z(self.x + 1, self.floor - 4))
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
        if target == (self.x, self.floor):
            self._arrive()
        else:
            self.target, self.state, self.left = target, "wiggle", 0.6

    def _leap(self) -> None:
        (x1, y1), x0, y0 = self.target, float(self.x), float(self.floor)
        span = math.hypot(x1 - x0, y1 - y0)
        self.hop = _Hop(x0, y0, x1, y1, 1.5 + span / 12, 0.6 + span / 30)
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

    def _cat(self) -> list[Cell]:
        breath = int(self.t * 1.6) % 2
        if self.state == "hop" and self.hop:
            facing = 1 if self.hop.x1 >= self.hop.x0 else -1
            return self._sprite(
                _JUMP, *self.hop.at(1 - max(0.0, self.left) / self.hop.flight), facing
            )
        if self.state == "sleep":
            breath = int(self.t * 0.8) % 2
            return self._sprite(_SLEEP[breath], self.x + 1, self.floor - 1, 1)
        if self.state in ("eat", "drink"):
            return self._sprite(_BOWL[breath], self.x, self.floor, 1)
        wiggle = self.state == "wiggle" and int(self.t * 8) % 2
        cells = self._sprite(_SIT[breath], self.x + wiggle, self.floor, 1)
        return cells + self._tail(_SIT[breath], self.x + wiggle)

    def _tail(self, pose: tuple[str, ...], x: int) -> list[Cell]:
        swish = _TAILS[int(self.t * 3) % len(_TAILS)]
        cells = []
        for i, piece in enumerate(swish):
            row = pose[i + 2]
            edge = x + len(row.rstrip())
            y = self.floor - 2 + i
            cells += [Cell(edge + j, y, g, _FUR) for j, g in enumerate(piece) if g != " "]
        return cells

    def _sprite(self, pose: tuple[str, ...], x: int, floor: int, facing: int) -> list[Cell]:
        """The pose standing on ``floor``; blanks inside its outline hide what is behind it."""
        cells = []
        top = floor - len(pose) + 1
        for dy, row in enumerate(pose):
            body = (row[::-1].translate(_MIRROR) if facing < 0 else row).rstrip()
            start = len(body) - len(body.lstrip())
            cells += [self._paint(x + dx, top + dy, g) for dx, g in enumerate(body) if dx >= start]
        return cells

    def _paint(self, x: int, y: int, glyph: str) -> Cell:
        blink = int(self.t * 8) % 37 == 0
        eating = self.state == "eat"
        bite = int(self.t * (3 if eating else 5)) % 2
        parts = {
            "E": ("-", _FUR) if blink else ("o", _EYE),
            "C": ("-", _FUR),
            "N": ("^", _PINK),
            "J": ("o" if bite else "-", _FUR) if eating else ("u" if bite else "-", _PINK),
            "L": ("\\", _RIM),
            "R": ("/", _RIM),
            "F": (":", _FOOD) if eating else ("-" if bite else "~", _WATER),
        }
        return Cell(x, y, *parts.get(glyph, (glyph, _FUR)))


__all__ = ["CatScene"]
