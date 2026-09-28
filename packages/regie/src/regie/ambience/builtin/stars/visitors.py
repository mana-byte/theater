"""Rare visitors crossing the stars: the contract they share, and the small ship."""

from __future__ import annotations

import random

from regie.ambience.api import Cell


class Visitor:
    """Something crossing the band; ``gone`` turns True once it has left for good."""

    gone = False

    def step(self, dt: float, stars: frozenset[tuple[int, int]]) -> set[tuple[int, int]]:
        """Move on; return the visible stars it ate on the way."""
        raise NotImplementedError

    def cells(self) -> list[Cell]:
        raise NotImplementedError

    def hull(self) -> set[tuple[int, int]]:
        """The cells its body covers, so stars behind it stay hidden."""
        raise NotImplementedError

    def resize(self, width: int, height: int) -> bool:
        """The band changed; False drops the visitor, which no longer fits."""
        raise NotImplementedError


class Ship(Visitor):
    body, style, speed = "<o=-", "$warning", 3.0

    def __init__(self, rng: random.Random, width: int, height: int) -> None:
        self.x, self.y = float(width), rng.randrange(height)

    def step(self, dt: float, stars: frozenset[tuple[int, int]]) -> set[tuple[int, int]]:
        self.x -= self.speed * dt
        self.gone = self.x <= -len(self.body)
        return set()

    def cells(self) -> list[Cell]:
        return [Cell(int(self.x) + i, self.y, g, self.style) for i, g in enumerate(self.body)]

    def hull(self) -> set[tuple[int, int]]:
        return {(int(self.x) + i, self.y) for i in range(len(self.body))}

    def resize(self, width: int, height: int) -> bool:
        return self.y < height


__all__ = ["Ship", "Visitor"]
