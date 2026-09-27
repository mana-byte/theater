"""The built-in ambiences selectable with ``[regie] tree_ambience``; user scenes come later."""

from __future__ import annotations

from types import MappingProxyType

from regie.ambience.scene import Scene
from regie.ambience.scenes.aquarium import AquariumScene
from regie.ambience.scenes.cat import CatScene
from regie.ambience.scenes.fire import FireScene
from regie.ambience.scenes.footer import FooterScene
from regie.ambience.scenes.leaves import LeavesScene
from regie.ambience.scenes.stars import StarsScene

#: Turns ambience off entirely.
NO_AMBIENCE = "none"
DEFAULT_AMBIENCE = "footer"

SCENES: MappingProxyType[str, type[Scene]] = MappingProxyType(
    {
        scene.name: scene
        for scene in (FooterScene, LeavesScene, FireScene, AquariumScene, StarsScene, CatScene)
    }
)

AMBIENCE_NAMES = frozenset({*SCENES, NO_AMBIENCE})


def scene_for(name: str) -> type[Scene] | None:
    """The scene class for a validated setting, or None when ambience is off."""
    return SCENES.get(name)


__all__ = ["AMBIENCE_NAMES", "DEFAULT_AMBIENCE", "NO_AMBIENCE", "SCENES", "scene_for"]
