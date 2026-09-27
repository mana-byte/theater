"""Leaves fall from under the last row down to the band's bottom.

Placeholder: draws nothing until its scene is implemented.
"""

from __future__ import annotations

from regie.ambience.scene import Cell, Phase, Scene


class LeavesScene(Scene):
    name = "leaves"
    min_rows = 2

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        return []


__all__ = ["LeavesScene"]
