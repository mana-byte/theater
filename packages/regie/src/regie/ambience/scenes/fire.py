"""Flames rise from the band's bottom, never too close to the tree.

A doom-fire lite: per-column heights that flicker, embers on the tips.
"""

from __future__ import annotations

import math
import random

from regie.ambience.scene import Cell, Phase, Scene

#: Bottom → tip look for a column of three or more cells: core, flame, ember tip.
_CORE = ("^", "$error")
_FLAME = ("*", "$warning")
_EMBER = (".", "$text-muted dim")


class FireScene(Scene):
    name = "fire"
    fps = 6.0
    min_rows = 2
    intro_seconds = 2.0
    outro_seconds = 2.0

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self._bases: list[int] = []

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self._bases = self._bases[:width]
        while len(self._bases) < width:
            self._bases.append(self._new_base())

    def _new_base(self) -> int:
        roll = self.rng.random()
        if roll < 0.3:
            return 0
        return 1 + int(self.rng.random() * 4)

    def _max_height(self) -> int:
        return max(1, int(self.height * 0.4))

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if phase is Phase.OUTRO and progress >= 1.0:
            return []
        if self.width <= 0 or self.height < self.min_rows:
            return []
        if phase is Phase.INTRO:
            scale = progress * progress
        elif phase is Phase.OUTRO:
            scale = math.sqrt(max(0.0, 1.0 - progress))
        else:
            scale = 1.0
        max_h = self._max_height()
        flicker = (-1, 0) if phase is Phase.OUTRO else (-1, 0, 1)  # a dying fire never flares
        cells: list[Cell] = []
        for x, base in enumerate(self._bases[: self.width]):
            cells.extend(self._column(x, base, scale, max_h, flicker))
        return cells

    def _column(
        self, x: int, base: int, scale: float, max_h: int, flicker: tuple[int, ...]
    ) -> list[Cell]:
        height = round(base * scale)
        if height <= 0:
            return []
        height = min(max_h, max(1, height + self.rng.choice(flicker)))
        cells = []
        for step in range(height):
            y = self.height - 1 - step
            if y < 0:
                break
            cells.append(Cell(x, y, *self._look(height, step)))
        return cells

    @staticmethod
    def _look(height: int, step: int) -> tuple[str, str]:
        if height == 1 or step == 0:
            return _EMBER
        if step == height - 1:
            return _CORE
        return _FLAME


__all__ = ["FireScene"]
