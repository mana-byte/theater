"""The ambiences ``[regie] tree_ambience`` can name: shipped ones, then the user's own plugins.

Both are package-manifest plugins, like Theater's: ``<name>/manifest.py`` exporting one
``AmbienceManifest``. A local plugin replaces a shipped one of the same name; a broken one is
reported, never fatal to Régie, unless it is the one the configuration asks for.
"""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType, ModuleType

from regie.ambience.manifest import AmbienceManifest
from regie.ambience.scene import Scene
from regie.paths import paths_from_environment
from theater.frontend.plugins import (
    LOCAL,
    SHIPPED,
    PackageCandidate,
    cleanup_package,
    discover_packages,
    import_manifest,
)

#: Turns ambience off entirely.
NO_AMBIENCE = "none"
DEFAULT_AMBIENCE = "footer"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SHIPPED_ROOT = Path(__file__).parent / "builtin"
_PACKAGE_PREFIX = "regie_ambience_pkg_"
_GUIDE = "packages/regie/README.md"


@dataclass(frozen=True, slots=True)
class AmbienceCatalog:
    """Every loadable scene by name, and why each broken plugin was skipped."""

    scenes: Mapping[str, type[Scene]]
    errors: Mapping[str, str]

    @property
    def names(self) -> frozenset[str]:
        return frozenset({*self.scenes, NO_AMBIENCE})


def load_catalog(local_root: Path) -> AmbienceCatalog:
    """Shipped ambiences, then ``local_root``'s, a local one replacing a shipped namesake."""
    scenes: dict[str, type[Scene]] = {}
    errors: dict[str, str] = {}
    for root, source in ((_SHIPPED_ROOT, SHIPPED), (local_root, LOCAL)):
        for candidate in discover_packages(
            root, source=source, kind="ambience", name_pattern=_NAME, guide=_GUIDE
        ):
            loaded = _load(candidate)
            if isinstance(loaded, str):
                errors[candidate.name] = loaded
                logging.getLogger("regie").warning(
                    "ambience %s skipped: %s", candidate.name, loaded
                )
            elif candidate.name != NO_AMBIENCE:
                scenes[candidate.name] = loaded
                errors.pop(candidate.name, None)
    return AmbienceCatalog(MappingProxyType(scenes), MappingProxyType(errors))


@cache
def catalog(local_root: Path) -> AmbienceCatalog:
    """The catalog with ``local_root``'s plugins, loaded once per process."""
    return load_catalog(local_root)


def scene_for(name: str) -> type[Scene] | None:
    """The scene class for a validated setting, or None when ambience is off."""
    return catalog(paths_from_environment().ambiences_dir).scenes.get(name)


def _load(candidate: PackageCandidate) -> type[Scene] | str:
    """The plugin's scene, or why it cannot be used."""
    if candidate.error is not None:
        return candidate.error
    manifest_path = candidate.path / "manifest.py"
    try:
        module = _import(candidate)
    except KeyboardInterrupt:
        raise
    except (Exception, SystemExit) as exc:
        return f"{manifest_path}: {exc!r}"
    manifest = getattr(module, "MANIFEST", None)
    if not isinstance(manifest, AmbienceManifest):
        return f"{manifest_path}: must end with `MANIFEST = AmbienceManifest(scene=...)`"
    scene = manifest.scene
    if not (isinstance(scene, type) and issubclass(scene, Scene)):
        return f"{manifest_path}: MANIFEST.scene must be a Scene subclass"
    if scene.name != candidate.name:
        return f"{manifest_path}: the scene is named {scene.name!r}, its folder {candidate.name!r}"
    return scene


def _import(candidate: PackageCandidate) -> ModuleType:
    """Shipped ambiences are ordinary Régie modules; local ones get an isolated package."""
    if candidate.source == SHIPPED:
        return importlib.import_module(f"regie.ambience.builtin.{candidate.name}.manifest")
    module, package = import_manifest(candidate.path, candidate.source, prefix=_PACKAGE_PREFIX)
    if not isinstance(getattr(module, "MANIFEST", None), AmbienceManifest):
        cleanup_package(package)
    return module


__all__ = [
    "DEFAULT_AMBIENCE",
    "NO_AMBIENCE",
    "AmbienceCatalog",
    "catalog",
    "load_catalog",
    "scene_for",
]
