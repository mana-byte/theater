"""A thin line at the band's bottom saying the tree is here; the default.

Placeholder: draws nothing until its scene is implemented.
"""

from __future__ import annotations

from regie.ambience.scene import Cell, Phase, Scene


class FooterScene(Scene):
    name = "footer"
    min_rows = 1

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        return []


__all__ = ["FooterScene"]
