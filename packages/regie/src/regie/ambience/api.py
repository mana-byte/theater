"""The public surface for ambience plugins: import from here and nowhere else in Régie.

A plugin is ``$THEATER_HOME/regie/plugins/<name>/manifest.py`` ending in
``MANIFEST = AmbienceManifest(scene=MyScene)``; sibling modules import each other relatively.
"""

from __future__ import annotations

from regie.ambience.manifest import AmbienceManifest
from regie.ambience.pixels import HALF_BLOCKS, Canvas, paint, to_cells
from regie.ambience.scene import MAX_TRANSITION_SECONDS, Cell, Phase, Scene

__all__ = [
    "HALF_BLOCKS",
    "MAX_TRANSITION_SECONDS",
    "AmbienceManifest",
    "Canvas",
    "Cell",
    "Phase",
    "Scene",
    "paint",
    "to_cells",
]
