"""``config.example.toml`` loads as it stands and names every shipped ambience."""

from __future__ import annotations

import re
from pathlib import Path

from regie.ambience.registry import NO_AMBIENCE, load_catalog
from regie.config import load_settings
from regie.contracts import RegieSettings

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_the_example_loads_with_the_defaults_and_lists_every_shipped_ambience() -> None:
    assert load_settings(EXAMPLE).tree_ambience == RegieSettings().tree_ambience
    listed = set(re.findall(r'^#\s+"([a-z-]+)"\s', EXAMPLE.read_text(), re.MULTILINE))
    assert listed == {*load_catalog(Path("/nonexistent")).scenes, NO_AMBIENCE}
