"""What an ambience plugin exports: ``MANIFEST = AmbienceManifest(scene=...)``."""

from __future__ import annotations

from dataclasses import dataclass

from regie.ambience.scene import Scene


@dataclass(frozen=True, slots=True)
class AmbienceManifest:
    """One ambience. Its name is its folder's name, and ``scene.name`` must match it."""

    scene: type[Scene]
    description: str = ""


__all__ = ["AmbienceManifest"]
