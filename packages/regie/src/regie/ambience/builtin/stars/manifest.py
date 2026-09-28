"""The shipped stars ambience."""

from regie.ambience.api import AmbienceManifest

from .scene import StarsScene

MANIFEST = AmbienceManifest(scene=StarsScene)
