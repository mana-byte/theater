"""Fish swim by below the tree, with bubbles and seaweed.

Placeholder: draws nothing until its scene is implemented.
"""

from __future__ import annotations

from regie.ambience.scene import Cell, Phase, Scene


class AquariumScene(Scene):
    name = "aquarium"
    min_rows = 2

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        return []


__all__ = ["AquariumScene"]
