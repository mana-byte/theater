"""The shipped fire ambience."""

from regie.ambience.api import AmbienceManifest

from .scene import FireScene

MANIFEST = AmbienceManifest(scene=FireScene)
