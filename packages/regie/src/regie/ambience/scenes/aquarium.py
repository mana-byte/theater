"""A small aquarium of passing fish, bubbles, and bottom-row seaweed."""

from __future__ import annotations

import random
from dataclasses import dataclass

from regie.ambience.scene import Cell, Phase, Scene

_FISH_PAIRS = (("><>", "<><"), ("><(((º>", "<º)))><"))
_FISH_STYLES = ("$secondary", "$accent dim", "$primary dim")
_BUBBLES = ("°", "o", "○", "·")


@dataclass(slots=True)
class _Fish:
    x: float
    y: int
    direction: int
    speed: float
    pair: tuple[str, str]
    style: str

    @property
    def sprite(self) -> str:
        return self.pair[0] if self.direction > 0 else self.pair[1]


@dataclass(slots=True)
class _Bubble:
    x: int
    y: float
    speed: float
    glyph: str
    pop_at: float


class AquariumScene(Scene):
    name = "aquarium"
    fps = 5.0
    min_rows = 2
    intro_seconds = 2.0
    outro_seconds = 1.5

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self._fish: list[_Fish] = []
        self._bubbles: list[_Bubble] = []
        self._bubble_elapsed = 0.0
        self._bubble_wait = self.rng.uniform(1.2, 2.2)
        self._elapsed = 0.0
        self._phase: Phase | None = None

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        lanes = self._lanes()
        target = max(1, min(3, len(lanes), (width + 5) // 14))
        self._fish = self._fish[:target]
        for index, fish in enumerate(self._fish):
            fish.y = lanes[min(len(lanes) - 1, index * len(lanes) // target)]
            self._fit_fish(fish)
        while len(self._fish) < target:
            self._fish.append(self._new_fish(len(self._fish), target, lanes))
        self._bubbles = [
            bubble for bubble in self._bubbles if 0 <= bubble.x < width and 0.5 < bubble.y < height
        ]

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if phase is Phase.OUTRO and progress >= 1.0:
            return []
        self._change_phase(phase)
        self._elapsed += max(0.0, dt)
        self._move_fish(phase, progress, dt)
        self._move_bubbles(phase, progress, dt)
        if phase is not Phase.OUTRO:
            self._spawn_bubbles(dt * (progress if phase is Phase.INTRO else 1.0))
        return [
            *self._seaweed(phase, progress),
            *self._bubble_cells(phase, progress),
            *self._fish_cells(),
        ]

    def _lanes(self) -> list[int]:
        if self.height <= 2:
            return [max(0, self.height - 1)]
        bottom = self.height - 1 if self.height >= 4 else self.height
        return list(range(1, bottom))

    def _new_fish(self, index: int, count: int, lanes: list[int]) -> _Fish:
        pairs = [pair for pair in _FISH_PAIRS if len(pair[0]) <= self.width]
        pair = self.rng.choice(pairs or ((">", "<"),))
        direction = self.rng.choice((-1, 1))
        lane = lanes[min(len(lanes) - 1, index * len(lanes) // count)]
        x = -float(len(pair[0])) if direction > 0 else float(self.width)
        return _Fish(
            x=x,
            y=lane,
            direction=direction,
            speed=self.rng.uniform(4.0, 5.5),
            pair=pair,
            style=self.rng.choice(_FISH_STYLES),
        )

    def _fit_fish(self, fish: _Fish) -> None:
        if len(fish.sprite) > self.width:
            fish.pair = (">", "<")
        span = len(fish.sprite)
        if fish.x >= self.width or fish.x + span <= 0:
            fish.x = -float(span) if fish.direction > 0 else float(self.width)
        else:
            fish.x = min(float(self.width - span), max(0.0, fish.x))

    def _change_phase(self, phase: Phase) -> None:
        if phase is self._phase:
            return
        if phase is Phase.OUTRO:
            for fish in self._fish:
                centre = fish.x + len(fish.sprite) / 2
                fish.direction = -1 if centre < self.width / 2 else 1
        elif phase is Phase.INTRO and self._phase is Phase.OUTRO:
            for fish in self._fish:
                fish.direction *= -1
        self._phase = phase

    def _move_fish(self, phase: Phase, progress: float, dt: float) -> None:
        for fish in self._fish:
            speed = fish.speed
            if phase is Phase.OUTRO:
                speed += (self.width + len(fish.sprite)) / self.outro_seconds
                speed *= 1.0 + progress
            fish.x += fish.direction * speed * max(0.0, dt)
            if phase is Phase.OUTRO:
                continue
            span = len(fish.sprite)
            if fish.direction > 0 and fish.x >= self.width:
                fish.x = -float(span)
            elif fish.direction < 0 and fish.x + span <= 0:
                fish.x = float(self.width)

    def _move_bubbles(self, phase: Phase, progress: float, dt: float) -> None:
        lift = 1.0 + (2.0 * progress if phase is Phase.OUTRO else 0.0)
        for bubble in self._bubbles:
            bubble.y -= bubble.speed * lift * max(0.0, dt)
        self._bubbles = [bubble for bubble in self._bubbles if bubble.y > 0.5]

    def _spawn_bubbles(self, elapsed: float) -> None:
        if self.height < 3:
            return
        self._bubble_elapsed += max(0.0, elapsed)
        while self._bubble_elapsed >= self._bubble_wait:
            self._bubble_elapsed -= self._bubble_wait
            self._bubble_wait = self.rng.uniform(1.2, 2.2)
            sources = [fish for fish in self._fish if fish.y >= 2 and self._visible(fish)]
            if not sources:
                continue
            fish = self.rng.choice(sources)
            head = fish.x + len(fish.sprite) - 1 if fish.direction > 0 else fish.x
            self._bubbles.append(
                _Bubble(
                    x=min(self.width - 1, max(0, round(head))),
                    y=fish.y - 0.2,
                    speed=self.rng.uniform(0.7, 1.2),
                    glyph=self.rng.choice(_BUBBLES),
                    pop_at=self.rng.uniform(0.35, 0.8),
                )
            )

    def _visible(self, fish: _Fish) -> bool:
        x = round(fish.x)
        return x >= 0 and x + len(fish.sprite) <= self.width

    def _fish_cells(self) -> list[Cell]:
        return [
            Cell(round(fish.x), fish.y, fish.sprite, fish.style)
            for fish in self._fish
            if self._visible(fish)
        ]

    def _bubble_cells(self, phase: Phase, progress: float) -> list[Cell]:
        return [
            Cell(bubble.x, round(bubble.y), bubble.glyph, "$text-muted")
            for bubble in self._bubbles
            if 0 < round(bubble.y) < self.height
            and (phase is not Phase.OUTRO or progress < bubble.pop_at)
        ]

    def _seaweed(self, phase: Phase, progress: float) -> list[Cell]:
        if self.height < 4:
            return []
        positions = range(min(4, self.width // 2), self.width, 8)
        cells = []
        for index, x in enumerate(positions):
            threshold = (index % 4 + 1) / 8
            if phase is Phase.INTRO and progress < threshold:
                continue
            if phase is Phase.OUTRO and progress >= 1.0 - threshold:
                continue
            sway = "(" if int(self._elapsed / 1.5 + index) % 2 else ")"
            cells.append(Cell(x, self.height - 1, sway, "$secondary dim"))
        return cells


__all__ = ["AquariumScene"]
