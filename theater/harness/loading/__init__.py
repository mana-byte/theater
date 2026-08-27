"""Package-manifest loader: discovery, isolated import, and compilation.

Discovers named plugin directories, imports each under an isolated synthetic
package, and compiles its ``MANIFEST`` into a runtime ``Harness``. Shipped and
local roots traverse the same loader; legacy single-file plugins are never
executed and receive an actionable migration diagnostic.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from theater.harness.loading.discovery import MANIFEST_FILENAME, discover
from theater.harness.loading.importer import PACKAGE_PREFIX, load_plugin
from theater.harness.loading.models import LOCAL, SHIPPED, LoadedPlugin, PluginError


def scan(directory: Path, *, source: str, skip: Iterable[str] = ()) -> list[LoadedPlugin]:
    """Discover and load every plugin in ``directory``, in name order.

    A missing directory returns an empty list. Disabled names are filtered
    before any import. Legacy top-level ``.py`` files are reported as broken
    results, never executed. Successful and broken results are both returned.
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
