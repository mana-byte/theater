"""Directory-name discovery for package-manifest plugins.

A visible directory without ``manifest.py`` is broken, not skipped; legacy ``.py`` files never run.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from theater.constants.core import HARNESS_NAME
from theater.harness.loading.models import LoadedPlugin
from theater.plugins.loading import MANIFEST_FILENAME, discover_packages


def discover(root: Path, *, source: str, skip: Iterable[str] = ()) -> list[LoadedPlugin]:
    """Return deterministic directory-name-order results for one root.

    A missing root returns an empty list. Disabled names are filtered
    before any import or side effect. Results are ordered by directory name.
    """
    candidates = discover_packages(
        root,
        source=source,
        kind="harness",
        name_pattern=HARNESS_NAME,
        skip=skip,
        guide="docs/harness-plugins.md",
    )
    return [
        LoadedPlugin(
            path=candidate.path,
            source=candidate.source,
            name=candidate.name,
            error=candidate.error,
        )
        for candidate in candidates
    ]


__all__ = ["MANIFEST_FILENAME", "discover"]
