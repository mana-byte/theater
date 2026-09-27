"""Leaves fall from under the last tree row down to the band's bottom."""

from __future__ import annotations

import random
from dataclasses import dataclass

from regie.ambience.scene import Cell, Phase, Scene

#: Glyphs with their cell span; the emoji are two cells wide.
_GLYPHS = (("❦", 1), ("❧", 1), ("⸙", 1), ("🍂", 2), ("🍁", 2), ("🍃", 2))
_NARROW = _GLYPHS[:3]
_STYLES = ("$warning dim", "$success dim", "$text-muted")


@dataclass(slots=True)
class _Leaf:
    x: float
    y: float
    span: int
    vy: float
    vx: float
    glyph: str
    style: str
    fade: float  # outro progress at which this leaf gives up
    rest: float  # seconds it sits on the bottom row before vanishing


class LeavesScene(Scene):
    """A sparse, slow drift of leaves; the spawn rate ramps in and the outro fades them out."""

    name = "leaves"
    fps = 5.0
    min_rows = 2
    intro_seconds = 2.5
    outro_seconds = 2.0

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self._leaves: list[_Leaf] = []
        self._spawn_credit = 0.0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self._leaves = [leaf for leaf in self._leaves if self._in_bounds(leaf)]
        if len(self._leaves) > self._target():
            self._leaves = self.rng.sample(
                self._leaves, self._target()
            )  # a smaller band, fewer leaves

    def _in_bounds(self, leaf: _Leaf) -> bool:
        return leaf.y < self.height and 0.0 <= leaf.x <= float(self.width - leaf.span)

    def _target(self) -> int:
        """Few at once: about one leaf per sixty cells, at least one, at most two dozen."""
        return max(1, min(self.width * self.height // 60, 24))

    def _spawn(self) -> None:
        glyph, span = self.rng.choice(_GLYPHS if self.width > 2 else _NARROW)
        self._leaves.append(
            _Leaf(
                x=self.rng.uniform(0.0, float(self.width - span)),
                y=0.0,
                span=span,
                vy=self.rng.uniform(0.9, 1.8),
                vx=self.rng.uniform(-0.6, 0.6),
                glyph=glyph,
                style=self.rng.choice(_STYLES),
                fade=self.rng.uniform(0.25, 0.75),
                rest=self.rng.uniform(1.5, 3.0),
            )
        )

    def _step(self, dt: float, speed: float) -> None:
        floor = self.height - 1
        for leaf in self._leaves:
            if leaf.y >= floor:
                leaf.rest -= dt
                continue
            leaf.y = min(floor, leaf.y + leaf.vy * speed * dt)
            leaf.x = min(max(leaf.x + leaf.vx * speed * dt, 0.0), float(self.width - leaf.span))
        self._leaves = [leaf for leaf in self._leaves if leaf.rest > 0.0]

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if self.height < self.min_rows or self.width <= 0:
            return []
        if phase is Phase.OUTRO:
            if progress >= 1.0:
                return []
            self._step(dt, speed=1.0 + 4.0 * progress)  # the band empties: fall faster, fade out
            self._leaves = [leaf for leaf in self._leaves if progress < leaf.fade]
        else:
            rate = self._target() / (self.height / 1.5)  # steady state keeps about target leaves
            if phase is Phase.INTRO:
                rate *= progress  # the first leaves begin falling: spawning ramps up
            self._spawn_credit += rate * dt
            self._step(dt, speed=1.0)
            while self._spawn_credit >= 1.0 and len(self._leaves) < self._target():
                self._spawn_credit -= 1.0
                self._spawn()
        return [Cell(int(leaf.x), int(leaf.y), leaf.glyph, leaf.style) for leaf in self._leaves]


__all__ = ["LeavesScene"]
