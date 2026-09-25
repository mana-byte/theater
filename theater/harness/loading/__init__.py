"""Package-manifest loader: discovery, isolated import, and compilation.

Shipped and local roots share one loader; legacy single-file plugins are never executed.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from theater.harness.loading.discovery import MANIFEST_FILENAME, discover
from theater.harness.loading.importer import PACKAGE_PREFIX, load_plugin
from theater.harness.loading.models import LOCAL, SHIPPED, LoadedPlugin, PluginError


def scan(directory: Path, *, source: str, skip: Iterable[str] = ()) -> list[LoadedPlugin]:
    """Discover and load every plugin in ``directory``, in name order.

    Disabled names are filtered before any import; legacy ``.py`` files are broken, never run.
    """
    results = discover(directory, source=source, skip=skip)
    return [load_plugin(r) for r in results]


__all__ = [
    "LOCAL",
    "MANIFEST_FILENAME",
    "PACKAGE_PREFIX",
    "SHIPPED",
    "LoadedPlugin",
    "PluginError",
    "discover",
    "load_plugin",
    "scan",
]
