"""Adapter registry; each ``adapters/<name>.py`` exports one ``ADAPTER``."""

from __future__ import annotations

import importlib
from pathlib import Path

from ..contract import Adapter

#: The shipped plugins, relative to the repository root.
PLUGINS = Path("theater/harness/builtin/plugins")
_ROOT = Path(__file__).resolve().parents[2]

#: One adapter per shipped plugin: the adapter, the plugin folder and the CLI binary share one name.
ADAPTER_NAMES = tuple(sorted(path.parent.name for path in (_ROOT / PLUGINS).glob("*/manifest.py")))


def load_adapter(name: str) -> Adapter:
    """Import ``adapters.<name>`` lazily and return its ``ADAPTER``."""
    if name not in ADAPTER_NAMES:
        raise KeyError(f"unknown harness {name!r}; known: {', '.join(ADAPTER_NAMES)}")
    module = importlib.import_module(f"{__package__}.{name}")
    adapter: Adapter = module.ADAPTER
    return adapter
