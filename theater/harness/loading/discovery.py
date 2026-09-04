"""Directory-name discovery for package-manifest plugins.

A candidate is a regular direct child directory whose name matches the
canonical harness syntax and contains ``manifest.py``. Hidden and
underscore-prefixed directories are ignored. A visible directory without
``manifest.py`` is a broken result, not silently skipped. Top-level legacy
``.py`` files are never executed and produce a migration diagnostic.
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
