"""Neutral package discovery and isolated manifest import."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import sys
import types
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from re import Pattern

MANIFEST_FILENAME = "manifest.py"
LOCAL = "local"
SHIPPED = "shipped"


@dataclass(frozen=True, slots=True)
class PackageCandidate:
    """One package directory discovered before any manifest import."""

    path: Path
    source: str
    name: str
    error: str | None = None


def discover_packages(
    root: Path,
    *,
    source: str,
    kind: str,
    name_pattern: Pattern[str],
    skip: Iterable[str] = (),
    guide: str | None = None,
) -> list[PackageCandidate]:
    """Discover package directories in deterministic order without importing them."""
    skipped = set(skip)
    if not root.is_dir():
        return []

    results: list[PackageCandidate] = []
    entries = sorted(
        (entry for entry in root.iterdir() if not entry.name.startswith(".")),
        key=lambda entry: entry.name,
    )
    for entry in entries:
        name = entry.name
        if name.startswith("_"):
            continue
        stem = entry.stem if entry.is_file() and name.endswith(".py") else name
        if stem in skipped:
            continue
        if entry.is_file() and name.endswith(".py"):
            results.append(_legacy_result(entry, source, kind, guide))
            continue
        if not entry.is_dir() or entry.is_file():
            continue
        if name_pattern.fullmatch(name) is None:
            results.append(
                PackageCandidate(
                    path=entry,
                    source=source,
                    name=name,
                    error=_with_guide(
                        f"{entry}: directory name {name!r} is not a valid {kind} name. "
                        "Use lowercase letters, digits, '_' or '-', starting with a letter or "
                        "digit",
                        guide,
                    ),
                )
            )
            continue
        manifest_path = entry / MANIFEST_FILENAME
        if not manifest_path.is_file():
            results.append(
                PackageCandidate(
                    path=entry,
                    source=source,
                    name=name,
                    error=_with_guide(
                        f"{entry}: has no {MANIFEST_FILENAME}. A plugin directory must contain "
                        f"{MANIFEST_FILENAME} exporting MANIFEST",
                        guide,
                    ),
                )
            )
            continue
        results.append(PackageCandidate(path=entry, source=source, name=name))
    return results


def synthetic_package_name(directory: Path, source: str, *, prefix: str) -> str:
    """Return a deterministic isolated package name for one package directory."""
    digest = hashlib.md5(str(directory.resolve()).encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{prefix}{source}_{digest}"


def import_manifest(
    directory: Path,
    source: str,
    *,
    prefix: str,
) -> tuple[types.ModuleType, str]:
    """Import one ``manifest.py`` under an isolated synthetic package."""
    pkg_name = synthetic_package_name(directory, source, prefix=prefix)
    cleanup_package(pkg_name)
    manifest_path = directory / MANIFEST_FILENAME
    try:
        module = _import_manifest(pkg_name, directory, manifest_path)
    except BaseException:
        cleanup_package(pkg_name)
        raise
    return module, pkg_name


def cleanup_package(pkg_name: str) -> None:
    """Remove one synthetic package and all of its descendants from ``sys.modules``."""
    prefix = f"{pkg_name}."
    for key in [key for key in list(sys.modules) if key == pkg_name or key.startswith(prefix)]:
        sys.modules.pop(key, None)


def _import_manifest(pkg_name: str, directory: Path, manifest_path: Path) -> types.ModuleType:
    _create_synthetic_package(pkg_name, directory)
    manifest_full = f"{pkg_name}.manifest"
    spec = importlib.util.spec_from_file_location(manifest_full, manifest_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {manifest_path}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = pkg_name
    sys.modules[manifest_full] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(manifest_full, None)
        raise
    return module


def _create_synthetic_package(pkg_name: str, directory: Path) -> types.ModuleType:
    existing = sys.modules.get(pkg_name)
    if existing is not None:
        return existing
    spec = importlib.machinery.ModuleSpec(pkg_name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(directory)]
    package = types.ModuleType(pkg_name)
    package.__spec__ = spec
    package.__path__ = [str(directory)]
    package.__package__ = pkg_name
    sys.modules[pkg_name] = package
    return package


def _legacy_result(path: Path, source: str, kind: str, guide: str | None) -> PackageCandidate:
    stem = path.stem
    return PackageCandidate(
        path=path,
        source=source,
        name=stem,
        error=_with_guide(
            f"{path}: legacy single-file plugin. Move {path.name} to {stem}/{MANIFEST_FILENAME} "
            f"and export MANIFEST for a {kind} package",
            guide,
        ),
    )


def _with_guide(message: str, guide: str | None) -> str:
    return message if guide is None else f"{message} — see {guide}"
