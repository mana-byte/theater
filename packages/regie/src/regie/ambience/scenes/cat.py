"""A fat orange pixel cat under the tree: it sits, eats, drinks, and hops off to nap on its bed.

Everything is pixel art in soft colours, two pixels per cell. Letters in the art are palette
keys; ``M`` (mouth) and ``e`` (eye) are painted per frame, so one pose serves every bite and
blink. Positions are in pixels: ``x`` is a column, ``floor`` the pixel row under its feet.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from regie.ambience.pixels import Canvas, paint, to_cells
from regie.ambience.scene import Cell, Phase, Scene

_PALETTE = {
    "o": "#b98050",  # fur
    "d": "#8a5d3b",  # darker fur: stripes, ear tips, tail tip
    "w": "#c9b597",  # cream muzzle and tummy
    "e": "#7f9a72",  # eye
    "k": "#3d332e",  # closed eye, open mouth
    "p": "#b08486",  # nose, inner ear, tongue
    "g": "#6f777d",  # bowl
    "f": "#7a5a3c",  # kibble
    "u": "#4f7189",  # water
    "U": "#6c8ea3",  # a ripple
    "B": "#76698c",  # bed
    "b": "#5c5272",  # bed, shaded
}
#: Sitting: the head bobs a pixel as the fat tummy swells on each breath.
_HEAD = (
    ".d........d.",
    ".oo......oo.",
    ".opo....opo.",
    ".ooodoodooo.",
    "ooeooooooeoo",
    "oooooppooooo",
    ".oowwMMwwoo.",
)
_TUMMY = (
    (
        ".oooowwwwoooo.",
        "ooooowwwwooooo",
        "oodoowwwwoodoo",
        "oooowwwwwwoooo",
        ".wwoo....ooww.",
    ),
    (
        ".ooooowwwwooooo.",
        "oooooowwwwoooooo",
        "ooodowwwwwwodooo",
        "ooooowwwwwwooooo",
        ".wwooo....oooww.",
    ),
)
_TAILS = (
    ("...o", "...o", "..o.", "oo.."),
    ("..o.", "...o", "...o", "oo.."),
    ("....", "..od", ".oo.", "oo.."),
)
#: Head down in its bowl, the jaw working.
_BOWL = (
    "..oooooooooo..",
    ".oooooooooooo.",
    "ooodoooooodooo",
    "oodpoooooopdoo",
    "oooooooooooooo",
    "oookkooookkooo",
    "ooooooppoooooo",
    "..gFwwMMwwFg..",
    "...gggggggg...",
)
_DISH = ("gFFFFFFFFg", ".gggggggg.")
#: Curled up asleep, its back rising and falling, the tail wrapped round its paws.
_SLEEP = (
    (
        "...........oooooo...",
        ".d....d..oooooooooo.",
        ".oo..oo.ooooodooooo.",
        ".oooooo.oooooodoooo.",
        "okkookkooooooooodooo",
        "ooopoooodoooooooooo.",
        ".owwwwoddddddddddoo.",
        "..ww.ww........dd...",
    ),
    (
        "....................",
        ".d....d....ooooooo..",
        ".oo..oo.ooooodoooo..",
        ".oooooo.oooooodoooo.",
        "okkookkooooooooodooo",
        "ooopoooodoooooooooo.",
        ".owwwwoddddddddddoo.",
        "..ww.ww........dd...",
    ),
)
_JUMP = (
    "............d..d",
    "...........ooooo",
    "od..oooooooooeoo",
    ".ooooooooooooopo",
    "...ooo.....oo...",
    "..oo.........oo.",
)
_BED = ("BBBBBBBBBBBBBBBBBBBB", "bbbbbbbbbbbbbbbbbbbb")
_WIDE, _TALL = 22, 12  # columns and pixel rows the cat needs to be drawn at all


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
        self.t = 0.0
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
        return self.width >= _WIDE and self.height * 2 >= _TALL

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
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
        for tries in range(80):
            x = self.rng.randrange(self.width - _WIDE + 1)
            floor = self.rng.randrange(_TALL - 1, self.height * 2)
            gap, rows = (_WIDE, _TALL) if tries < 40 else (11, 2)  # a small band: bowls apart
            if all(abs(x - tx) >= gap or abs(floor - ty) >= rows for tx, ty in taken):
                break
        return x, floor

    def _spot_ok(self, x: int, floor: int) -> bool:
        return 0 <= x <= self.width - _WIDE and _TALL - 1 <= floor < self.height * 2

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if not self._fits or (phase is Phase.OUTRO and progress >= 1):
            return []
        self.t += dt
        if phase is Phase.IDLE:
            self._live(dt)
        self._drift_zs(dt)
        canvas: Canvas = {}
        self._furniture(canvas, phase, progress)
        if phase is Phase.IDLE:
            self._cat(canvas)
        else:  # it drops in from above, and leaps away up out of the band
            fall = 1 - progress if phase is Phase.INTRO else progress
            paint(canvas, _JUMP, self.x, round(self.floor - 5 - fall * (self.floor + 6)), _PALETTE)
        snores = [Cell(round(z.x), round(z.y), "zZz"[int(z.age)], "$text-muted") for z in self.zs]
        return to_cells(canvas) + snores

    def _furniture(self, canvas: Canvas, phase: Phase, progress: float) -> None:
        if phase is Phase.OUTRO and progress > 0.6:
            return
        ripple = "U" if int(self.t * 4) % 2 else "u"
        for (x, floor), fill in ((self.food, "f"), (self.water, ripple)):
            paint(canvas, _DISH, x + 2, floor - 1, {**_PALETTE, "F": _PALETTE[fill]})
        if self.has_bed:
            size = len(_BED[0])
            grown = size if self.state != "bed" else int(size * (1 - max(0.0, self.left) / 0.6))
            x, floor = self.bed
            start = (size - grown) // 2
            paint(canvas, tuple(row[:grown] for row in _BED), x + start, floor - 1, _PALETTE)

    def _live(self, dt: float) -> None:
        self.left -= dt
        if self.state == "hop" and self.hop:
            if self.left <= 0:
                self.x, self.floor = self.hop.at(1.0)
                self.hop = None
                self._arrive()
            return
        if self.state == "sleep" and int(self.t / 1.3) != int((self.t - dt) / 1.3):
            self.zs.append(_Z(self.x + 3, (self.floor - 10) / 2))
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
        span = math.hypot(x1 - x0, (y1 - y0) / 2)
        self.hop = _Hop(x0, y0, x1, y1, 4 + span / 4, 0.6 + span / 30)
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
        x, floor = self.x, self.floor
        if self.state == "hop" and self.hop:
            hx, feet = self.hop.at(1 - max(0.0, self.left) / self.hop.flight)
            paint(canvas, _JUMP, hx, feet - 5, _PALETTE, flip=self.hop.x1 < self.hop.x0)
        elif self.state == "sleep":
            pose = _SLEEP[int(self.t * 0.8) % 2]
            paint(canvas, pose, x, floor - 9, _PALETTE)
        elif self.state in ("eat", "drink"):
            bite = int(self.t * (3 if self.state == "eat" else 5)) % 2
            eating = self.state == "eat"
            mouth = ("k" if eating else "p") if bite else "w"
            fill = "f" if eating else "U" if bite else "u"
            parts = {"M": _PALETTE[mouth], "F": _PALETTE[fill]}
            paint(canvas, _BOWL, x, floor - 8, {**_PALETTE, **parts})
        else:
            self._sit(canvas, x + (self.state == "wiggle" and int(self.t * 8) % 2), floor)

    def _sit(self, canvas: Canvas, x: int, floor: int) -> None:
        breath = int(self.t * 1.6) % 2
        blink = int(self.t * 8) % 37 == 0
        tummy = _TUMMY[breath]
        paint(canvas, _TAILS[int(self.t * 3) % len(_TAILS)], x + 15 + breath, floor - 5, _PALETTE)
        paint(canvas, tummy, x + 1 - breath, floor - 4, _PALETTE)
        face = {**_PALETTE, "M": _PALETTE["w"], "e": _PALETTE["k" if blink else "e"]}
        paint(canvas, _HEAD, x + 2, floor - 11 + breath, face)


__all__ = ["CatScene"]
