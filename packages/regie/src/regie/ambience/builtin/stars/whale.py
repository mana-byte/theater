"""The star whale: a constellation that roams the sky, mouth wide, until it has eaten every star.

Hand-made bodies (flukes and fin beating) and heads (raised, level, lowered; mouth shut or
wide) are joined at the neck and mirrored, so it always faces and looks where it goes.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from regie.ambience.api import Cell

from .visitors import Visitor

#: The body behind the neck, per fluke beat (level, up, down); the fin beats with the flukes.
_BODIES = (
    (
        r"                *----+",
        r"   *        .--'",
        r"    \.  .--'    +    +",
        r"    *:<'   +         +",
        r"    /' `--.___________",
        r"                 `\*",
    ),
    (
        r"  *\            *----+",
        r"    \.      .--'",
        r"     :. .--'    +    +",
        r"      <'   +         +",
        r"       `--.___________",
        r"                 `|*",
    ),
    (
        r"                *----+",
        r"            .--'",
        r"        .--'    +    +",
        r"      <'   +         +",
        r"     :'`--.___________",
        r"   */            `/*",
    ),
)
#: The head per (pitch, mouth open): raised to climb, lowered to dive, so it looks where it goes.
#: ``+`` marks shimmering insides, ``o`` the eye, ``~`` the open mouth (drawn empty, it eats).
_HEADS = {
    (0, False): ("----*", "     `--.", "    +  o `*", "     ____/", "_______.-*", ""),
    (0, True): (
        "----*",
        "     `--.",
        "    +  o `*",
        "    ~~~~~~~~~",
        "____`.~~~~~~~~",
        "        `-.__.-*",
    ),
    (-1, False): ("----*--.__", "        o `*", "    +   ___/", "     _.-'", "__.-*", ""),
    (-1, True): (
        "----*--.__",
        "        o `*",
        "    +  ~~~~~~~",
        "     .~~~~~~~~",
        "__.-' `-.__.-*",
        "",
    ),
    (1, False): (
        "----*",
        "     `--.",
        "    +    `--.",
        "        o   `*",
        "______.  ____/",
        "        `'",
    ),
    (1, True): (
        "----*",
        "     `--.",
        "    +    `--.",
        "        o   `*",
        "______ ~~~~~~~~",
        "      `-.__.-*",
    ),
}
_NECK, _BEATS = 22, (0, 1, 0, 2)
_MIRROR = str.maketrans("/\\<>()`'", "\\/><)('`")
_SHIMMER, _GLOW = "*+.'", "$success"
_WIDTH, _HEIGHT, _MOUTH_ROW = 37, 6, 3.5
_BLOWHOLE = 21  # the top node, where stardust spouts

Glyphs = tuple[tuple[int, int, str], ...]


def _glyphs(beat: int, pitch: int, mouth: bool, heading: int) -> Glyphs:
    body, head = _BODIES[_BEATS[beat]], _HEADS[(pitch, mouth)]
    rows = [(body[r].ljust(_NECK) + head[r]).ljust(_WIDTH) for r in range(_HEIGHT)]
    if heading < 0:
        rows = [row[::-1].translate(_MIRROR) for row in rows]
    return tuple((x, y, g) for y, row in enumerate(rows) for x, g in enumerate(row) if g != " ")


#: Every drawable variant, keyed by (heading, fluke beat, pitch, mouth open).
_POSES = {
    (heading, beat, pitch, mouth): _glyphs(beat, pitch, mouth, heading)
    for heading in (1, -1)
    for beat in range(len(_BEATS))
    for pitch in (-1, 0, 1)
    for mouth in (False, True)
}


@dataclass(slots=True)
class _Mote:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    style: str
    age: float = 0.0

    def cell(self) -> Cell:
        share = self.age / self.life
        glyph = "*" if share < 0.35 else "+" if share < 0.7 else "."
        return Cell(int(self.x), round(self.y), glyph, self.style)


class StarWhale(Visitor):
    """Sweeps pass after pass, steering at stars ahead, turning unseen past the edges."""

    rows_needed, speed, climb = _HEIGHT, 3.5, 1.6  # columns and rows per second
    max_feeding = 300.0

    def __init__(self, rng: random.Random, width: int, height: int) -> None:
        self.rng, self.width, self.height = rng, width, height
        self.heading = rng.choice((1, -1))
        self.x = -float(_WIDTH) if self.heading > 0 else float(width)
        self.y = rng.uniform(-2, max(-2, height - 4))
        self.t = self.vy = self.pause = self.chomp = 0.0
        self.target: tuple[int, int] | None = None
        self.full = self.gone = False
        self.glow: tuple[int, float] | None = None  # which inner star, how long it still glows
        self.motes: list[_Mote] = []
        self._wake_due = self._spout_due = 0.0

    def resize(self, width: int, height: int) -> bool:
        self.width, self.height = width, height
        return height >= self.rows_needed

    @property
    def pitch(self) -> int:
        return -1 if self.vy < -0.5 else 1 if self.vy > 0.5 else 0

    def _placed(self, glyphs: Glyphs) -> list[tuple[int, int, str]]:
        left, top = int(self.x), round(self.y)
        return [(left + x, top + y, g) for x, y, g in glyphs]

    def _pose(self) -> list[tuple[int, int, str]]:
        mouth = not self.full and self.chomp <= 0
        return self._placed(_POSES[(self.heading, int(self.t * 2.5) % 4, self.pitch, mouth)])

    def step(self, dt: float, stars: frozenset[tuple[int, int]]) -> set[tuple[int, int]]:
        self.t += dt
        self._age_motes(dt)
        if self.pause > 0:
            self.pause -= dt
            return set()
        self.full = self.full or not stars or self.t > self.max_feeding
        self._steer(dt, stars)
        self.x += self.heading * self.speed * dt
        self.y += self.vy * dt
        self.chomp -= dt
        if self.glow:
            self.glow = (self.glow[0], self.glow[1] - dt) if self.glow[1] > dt else None
        if self._out_of_sight():
            self._turn()
        else:
            self._shed(dt)
        return self._eat(stars) if not self.full else set()

    def _steer(self, dt: float, stars: frozenset[tuple[int, int]]) -> None:
        mouth_x = self.x + (_WIDTH - 5 if self.heading > 0 else 5)
        if self.target not in stars or (self.target[0] - mouth_x) * self.heading < 0:
            ahead = [s for s in stars if (s[0] - mouth_x) * self.heading >= 0]
            ahead.sort(key=lambda s: abs(s[0] - mouth_x) + 4 * abs(s[1] - self.y - _MOUTH_ROW))
            self.target = self.rng.choice(ahead[:3]) if ahead else None
        goal = self.target[1] - _MOUTH_ROW if self.target else self.y
        goal = min(max(goal, -_MOUTH_ROW), self.height - 4.0)  # the mouth reaches every row
        wander = 0.4 * math.sin(self.t * 0.9)
        self.vy += ((goal - self.y) * 0.6 + wander - self.vy) * min(1.0, 1.2 * dt)
        self.vy = min(max(self.vy, -self.climb), self.climb)

    def _out_of_sight(self) -> bool:
        return self.x > self.width if self.heading > 0 else self.x < -_WIDTH

    def _turn(self) -> None:
        if self.full:
            self.gone = not self.motes
            return
        self.heading = -self.heading
        self.x = float(self.width) if self.heading < 0 else -float(_WIDTH)
        self.pause, self.target = self.rng.uniform(1.0, 4.0), None

    def _eat(self, stars: frozenset[tuple[int, int]]) -> set[tuple[int, int]]:
        pose = _POSES[(self.heading, 0, self.pitch, True)]
        mouth = {(x, y) for x, y, g in self._placed(pose) if g == "~"}
        eaten = mouth & stars
        if eaten:
            self.chomp = 0.35
            self.glow = (self.rng.randrange(5), 2.5)  # five inner stars
        return set(eaten)

    def _age_motes(self, dt: float) -> None:
        for mote in self.motes:
            mote.age += dt
            mote.x, mote.y = mote.x + mote.vx * dt, mote.y + mote.vy * dt
        self.motes = [m for m in self.motes if m.age < m.life]

    def _shed(self, dt: float) -> None:
        tail = self.x + (3 if self.heading > 0 else _WIDTH - 3)
        self._wake_due -= dt
        if self._wake_due <= 0:
            self._wake_due = 0.3
            y = self.y + 3 + self.rng.uniform(-1.0, 1.0)
            self.motes.append(_Mote(tail, y, 0.0, 0.0, 3.0, "$secondary"))
        self._spout_due -= dt
        if self._spout_due <= 0 and self.y >= 1:
            self._spout_due = 6.0
            hole = self.x + (_BLOWHOLE if self.heading > 0 else _WIDTH - 1 - _BLOWHOLE)
            for vx in (-1.5, -0.5, 0.5, 1.5):
                self.motes.append(_Mote(hole, self.y - 0.5, vx, -2.0, 1.2, "$text"))

    def cells(self) -> list[Cell]:
        cells = [mote.cell() for mote in self.motes]
        if self.pause > 0 or self._out_of_sight():
            return cells
        inner = 0
        for x, y, g in self._pose():
            if g == "+":
                glowing = self.glow is not None and self.glow[0] == inner
                inner += 1
                shimmer = _SHIMMER[(x * 7 + y * 3 + int(self.t * 3)) % 4]
                cells.append(Cell(x, y, "*" if glowing else shimmer, _GLOW if glowing else "$text"))
            elif g != "~":
                style = "$text" if g in "*o" else "$primary"
                cells.append(Cell(x, y, g, style))
        return cells

    def hull(self) -> set[tuple[int, int]]:
        if self.pause > 0 or self._out_of_sight():
            return set()
        spans: dict[int, list[int]] = {}
        for x, y, _ in self._pose():
            spans.setdefault(y, []).append(x)
        return {(x, y) for y, xs in spans.items() for x in range(min(xs), max(xs) + 1)}


__all__ = ["StarWhale"]
