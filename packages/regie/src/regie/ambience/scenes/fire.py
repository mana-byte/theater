"""Flames rise from the band's bottom, never too close to the tree.

Placeholder: draws nothing until its scene is implemented.
"""

from __future__ import annotations

from regie.ambience.scene import Cell, Phase, Scene


class FireScene(Scene):
    name = "fire"
    min_rows = 2

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        return []


__all__ = ["FireScene"]
