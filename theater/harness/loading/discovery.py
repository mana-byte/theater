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

MANIFEST_FILENAME = "manifest.py"


def discover(root: Path, *, source: str, skip: Iterable[str] = ()) -> list[LoadedPlugin]:
    """Return deterministic directory-name-order results for one root.

    A missing root returns an empty list. Disabled names are filtered
    before any import or side effect. Results are ordered by directory name.
    """
    skipped = set(skip)
    if not root.is_dir():
        return []

    results: list[LoadedPlugin] = []

    entries = sorted(
        (entry for entry in root.iterdir() if not entry.name.startswith(".")),
        key=lambda e: e.name,
    )

    for entry in entries:
        name = entry.name
        if name.startswith("_"):
            continue
        stem = entry.stem if entry.is_file() and name.endswith(".py") else name
        if stem in skipped:
            continue

        if entry.is_file() and name.endswith(".py"):
            results.append(_legacy_result(entry, source))
            continue

        if entry.is_file():
            continue

        if not entry.is_dir():
            continue

        if not HARNESS_NAME.fullmatch(name):
            results.append(
                LoadedPlugin(
                    path=entry,
                    source=source,
                    name=name,
                    error=f"{entry}: directory name {name!r} is not a valid harness name. "
                    "Use lowercase letters, digits, '_' or '-', starting with a "
                    "letter or digit — see docs/harness-plugins.md",
                )
            )
            continue

        manifest_path = entry / MANIFEST_FILENAME
        if not manifest_path.is_file():
            results.append(
                LoadedPlugin(
                    path=entry,
                    source=source,
                    name=name,
                    error=f"{entry}: has no {MANIFEST_FILENAME}. "
                    f"A plugin directory must contain {MANIFEST_FILENAME} exporting MANIFEST "
                    "— see docs/harness-plugins.md",
                )
            )
            continue

        results.append(LoadedPlugin(path=entry, source=source, name=name))

    return results


def _legacy_result(path: Path, source: str) -> LoadedPlugin:
    stem = path.stem
    return LoadedPlugin(
        path=path,
        source=source,
        name=stem,
        error=f"{path}: legacy single-file plugin. "
        f"Move {path.name} to {stem}/manifest.py and export MANIFEST "
        "— see docs/harness-plugins.md",
    )


__all__ = ["MANIFEST_FILENAME", "discover"]
