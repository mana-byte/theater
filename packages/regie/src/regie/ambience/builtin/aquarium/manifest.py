"""The shipped aquarium ambience."""

from regie.ambience.api import AmbienceManifest

from .scene import AquariumScene

MANIFEST = AmbienceManifest(scene=AquariumScene)
