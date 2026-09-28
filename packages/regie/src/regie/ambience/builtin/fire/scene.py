"""A campfire at the band's bottom: a doom-fire heat field whose rising heat turns to smoke.

Heat is fed along the bottom row, climbs one row per step, drifts sideways and cools; each
cell's heat picks its glyph and theme colour. Wisps of smoke rise from the tips and fade.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from regie.ambience.api import Cell, Phase, Scene

#: Hottest to coolest. Fire keeps its own ANSI colours: theme slots are not fire-coloured in
#: every theme (ansi-dark's accent is green), but the terminal's palette still tunes them.
_HEAT: tuple[tuple[str, str], ...] = (
    ("#", "bold ansi_bright_white"),
    ("@", "bold ansi_bright_yellow"),
    ("%", "ansi_bright_yellow"),
    ("*", "ansi_yellow"),
    ("+", "bold ansi_bright_red"),
    ("=", "ansi_bright_red"),
    (":", "ansi_red"),
    (".", "ansi_red"),
)
#: Smoke is grey, never a fire colour, so the two always read apart.
_SMOKE: tuple[tuple[str, str], ...] = (
    ("(", "ansi_bright_black"),
    (")", "ansi_bright_black"),
    ("~", "ansi_bright_black"),
    ("-", "ansi_bright_black dim"),
)
_MAX_HEAT = len(_HEAT)
#: A small campfire whatever the band's size: at most this many rows of flame, and never
#: more than half the band; the bed is at most this many columns wide.
_MAX_FLAME_ROWS = 6
_FLAME_SHARE = 0.5
_MAX_BED_COLUMNS = 14
_BED_SHARE = 0.5
#: Heat this low is drawn as nothing: it keeps the air above the flames clear.
_VISIBLE_HEAT = 2


@dataclass(slots=True)
class _Wisp:
    x: float
    y: float
    life: float  # seconds left
    drift: float


class FireScene(Scene):
    name = "fire"
    fps = 8.0
    min_rows = 2
    intro_seconds = 2.5
    outro_seconds = 3.0

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self._heat: list[list[int]] = []
        self._wisps: list[_Wisp] = []

    def resize(self, width: int, height: int) -> None:
        old = self._heat
        super().resize(width, height)
        # Keep the burning bottom rows when the band changes; new rows start cold.
        self._heat = [[0] * width for _ in range(height)]
        for back in range(1, min(len(old), height) + 1):
            row = old[-back]
            self._heat[-back][: min(width, len(row))] = row[:width]
        self._wisps = [w for w in self._wisps if 0 <= w.x < width and 0 <= w.y < height]

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if phase is Phase.OUTRO and progress >= 1.0:
            return []
        if self.width <= 0 or self.height < self.min_rows:
            return []
        fuel = {Phase.INTRO: progress, Phase.IDLE: 1.0, Phase.OUTRO: 1.0 - progress}[phase]
        self._feed(fuel)
        self._rise()
        self._smoke(phase, fuel, dt)
        return [*self._wisp_cells(), *self._flame_cells()]

    def _feed(self, fuel: float) -> None:
        """Stoke the bottom row: a hot bed in the middle that thins out toward the edges."""
        bottom = self._heat[-1]
        half = max(1.0, min(_MAX_BED_COLUMNS, self.width * _BED_SHARE) / 2)
        for x in range(self.width):
            centre = max(0.0, 1.0 - abs(x - (self.width - 1) / 2) / half)
            chance = fuel * min(1.0, 1.3 * centre)
            bottom[x] = _MAX_HEAT if self.rng.random() < chance else 0

    def _rise(self) -> None:
        """Doom fire: each cell takes the heat below it, cooled and nudged sideways.

        Cooling is tuned so tongues reach ``_MAX_FLAME_ROWS``, or half a short band.
        """
        reach = max(1, min(_MAX_FLAME_ROWS, round(self.height * _FLAME_SHARE)))
        mean_cooling = (_MAX_HEAT - _VISIBLE_HEAT) / reach  # heat lost per row, on average
        ceiling = self.height - 1 - round(reach * 1.5)  # a lucky tongue still stops here
        for y in range(self.height - 1):
            for x in range(self.width):
                below = self._heat[y + 1][x] if y > ceiling else 0
                # Stochastic rounding keeps the average cooling exact in tall bands too.
                spread = self.rng.random() * 2 * mean_cooling
                cooling = int(spread) + (self.rng.random() < spread - int(spread))
                target = min(self.width - 1, max(0, x + self.rng.randrange(-1, 2)))
                self._heat[y][target] = max(0, below - cooling)

    def _smoke(self, phase: Phase, fuel: float, dt: float) -> None:
        for wisp in self._wisps:
            wisp.y -= 1.6 * dt
            wisp.x = min(self.width - 1.0, max(0.0, wisp.x + wisp.drift * dt))
            wisp.life -= dt
        self._wisps = [w for w in self._wisps if w.life > 0 and w.y >= 0]
        if phase is Phase.OUTRO or self.height < 3:
            return  # a dying fire stops smoking; two rows have no room for smoke
        for x in range(self.width):
            tips = [y for y in range(self.height) if self._heat[y][x] > _VISIBLE_HEAT]
            if tips and self.rng.random() < 0.012 * fuel:
                self._wisps.append(
                    _Wisp(
                        x=float(x),
                        y=float(max(0, min(tips) - 1)),
                        life=self.rng.uniform(1.0, 2.0),
                        drift=self.rng.uniform(-0.8, 0.8),
                    )
                )

    def _flame_cells(self) -> list[Cell]:
        cells = []
        for y, row in enumerate(self._heat):
            for x, heat in enumerate(row):
                if heat > _VISIBLE_HEAT:
                    glyph, style = _HEAT[_MAX_HEAT - heat]
                    cells.append(Cell(x, y, glyph, style))
        return cells

    def _wisp_cells(self) -> list[Cell]:
        return [
            Cell(int(w.x), int(w.y), *_SMOKE[min(len(_SMOKE) - 1, int(2.0 - w.life) + 1)])
            for w in self._wisps
            if 0 <= int(w.y) < self.height
        ]


__all__ = ["FireScene"]
