"""Adapter registry; each ``adapters/<name>.py`` exports one ``ADAPTER``."""

from __future__ import annotations

import importlib

from ..contract import Adapter

#: Canonical plugin names: the adapter, the plugin folder and the CLI binary share one name.
ADAPTER_NAMES = ("claude", "codex", "opencode", "pi", "vibe")


def load_adapter(name: str) -> Adapter:
    """Import ``adapters.<name>`` lazily and return its ``ADAPTER``."""
    if name not in ADAPTER_NAMES:
        raise KeyError(f"unknown harness {name!r}; known: {', '.join(ADAPTER_NAMES)}")
    module = importlib.import_module(f"{__package__}.{name}")
    adapter: Adapter = module.ADAPTER
    return adapter
