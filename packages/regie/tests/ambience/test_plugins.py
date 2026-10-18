"""Ambiences are package-manifest plugins: shipped ones, and the user's own beside config.toml."""

from __future__ import annotations

from pathlib import Path

import pytest
from regie.ambience.registry import load_catalog
from regie.config import SettingsError, load_settings

_SCENE = """
from regie.ambience.api import Cell, Phase, Scene

class Glow(Scene):
    name = "{name}"

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        done = phase is Phase.OUTRO and progress >= 1
        return [] if done else [Cell(0, 0, "*")]
"""
_MANIFEST = """
from regie.ambience.api import AmbienceManifest

from .glow import Glow

MANIFEST = AmbienceManifest(scene=Glow)
"""


def _plugin(root: Path, name: str, manifest: str = _MANIFEST) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "glow.py").write_text(_SCENE.format(name=name))
    (root / name / "manifest.py").write_text(manifest)


def test_a_local_plugin_joins_the_shipped_ambiences_and_replaces_its_namesake(
    tmp_path: Path,
) -> None:
    _plugin(tmp_path, "glow")
    _plugin(tmp_path, "fire")  # the user's own fire wins over the shipped one
    catalog = load_catalog(tmp_path)
    assert {"footer", "stars", "glow", "fire"} <= set(catalog.scenes)
    assert catalog.scenes["fire"].__name__ == "Glow" and not catalog.errors


def test_a_broken_plugin_is_skipped_with_its_reason_and_refused_only_when_chosen(
    tmp_path: Path,
) -> None:
    home = tmp_path / "regie"
    _plugin(home / "plugins", "empty", manifest="MANIFEST = None\n")
    (home / "plugins" / "bare").mkdir()  # no manifest.py at all
    catalog = load_catalog(home / "plugins")
    assert set(catalog.errors) == {"empty", "bare"} and "footer" in catalog.scenes

    (home / "config.toml").write_text('[regie]\ntree_ambience = "empty"\n')
    with pytest.raises(SettingsError, match=r"broken ambience.*AmbienceManifest"):
        load_settings(home / "config.toml")
    (home / "config.toml").write_text('[regie]\ntree_ambience = "stars"\n')
    assert load_settings(home / "config.toml").tree_ambience == "stars"
