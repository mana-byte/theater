"""A small starry sky under the tree: twinkles, a rare shooting star.

Placeholder: draws nothing until its scene is implemented.
"""

from __future__ import annotations

from regie.ambience.scene import Cell, Phase, Scene


class StarsScene(Scene):
    name = "stars"
    min_rows = 2

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        return []


__all__ = ["StarsScene"]
