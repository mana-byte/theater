"""Isolated synthetic-package import for one plugin directory.

Each plugin is imported under a synthetic package name derived from source
and resolved directory path, so same-named sibling modules in separate
plugins cannot collide and ``sys.path`` is never mutated. Relative imports
(``from .parser import decode``, ``from . import helpers``) resolve within
the plugin directory because the synthetic package has a real ``__path__``.

On failure the synthetic package, its manifest submodule, and every
descendant module inserted for that package are removed from
``sys.modules``. KeyboardInterrupt is preserved after cleanup.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path

from theater.harness.contracts.manifest import HarnessManifest
from theater.harness.loading.discovery import MANIFEST_FILENAME
from theater.harness.loading.models import LoadedPlugin
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.validation import ManifestValidationError

PACKAGE_PREFIX = "theater_harness_pkg_"


def _synthetic_package_name(directory: Path, source: str) -> str:
    digest = hashlib.md5(str(directory.resolve()).encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{PACKAGE_PREFIX}{source}_{digest}"


def load_plugin(plugin: LoadedPlugin) -> LoadedPlugin:
    """Import and compile one discovered plugin, returning an updated result.

    Plugins with a pre-existing error (legacy, missing manifest) are returned
    unchanged. On success the ``harness`` field is set; on failure ``error``
    is set with actionable prose and the manifest path.
    """
    if plugin.error is not None:
        return plugin

    directory = plugin.path
    name = plugin.name
    manifest_path = directory / MANIFEST_FILENAME
    pkg_name = _synthetic_package_name(directory, plugin.source)

    _cleanup_package(pkg_name)

    try:
        manifest_module = _import_manifest(pkg_name, directory, manifest_path)
    except KeyboardInterrupt:
        _cleanup_package(pkg_name)
        raise
    except (Exception, SystemExit) as exc:
        _cleanup_package(pkg_name)
        return LoadedPlugin(
            path=directory,
            source=plugin.source,
            name=name,
            error=f"{manifest_path}: {exc!r}",
        )

    manifest = getattr(manifest_module, "MANIFEST", None)
    if manifest is None:
        _cleanup_package(pkg_name)
        return LoadedPlugin(
            path=directory,
            source=plugin.source,
            name=name,
            error=f"{manifest_path}: defines no MANIFEST. "
            "A plugin must end with `MANIFEST = HarnessManifest(...)` "
            "— see docs/harness-plugins.md",
        )
    if isinstance(manifest, type):
        _cleanup_package(pkg_name)
        return LoadedPlugin(
            path=directory,
            source=plugin.source,
            name=name,
            error=f"{manifest_path}: MANIFEST is the class {manifest.__name__}, "
            f"not an instance. Use `MANIFEST = {manifest.__name__}()`",
        )
    if not isinstance(manifest, HarnessManifest):
        _cleanup_package(pkg_name)
        return LoadedPlugin(
            path=directory,
            source=plugin.source,
            name=name,
            error=f"{manifest_path}: MANIFEST is a {type(manifest).__name__}, "
            "which is not a HarnessManifest — see docs/harness-plugins.md",
        )

    try:
        harness = compile_manifest(name, manifest)
    except KeyboardInterrupt:
        _cleanup_package(pkg_name)
        raise
    except ManifestValidationError as exc:
        _cleanup_package(pkg_name)
        return LoadedPlugin(
            path=directory,
            source=plugin.source,
            name=name,
            error=f"{manifest_path}: {exc}",
        )
    except (Exception, SystemExit) as exc:
        _cleanup_package(pkg_name)
        return LoadedPlugin(
            path=directory,
            source=plugin.source,
            name=name,
            error=f"{manifest_path}: compilation failed: {exc!r}",
        )

    return LoadedPlugin(
        path=directory,
        source=plugin.source,
        name=name,
        harness=harness,
    )


def _import_manifest(pkg_name: str, directory: Path, manifest_path: Path) -> types.ModuleType:
    """Create the synthetic package and import its manifest submodule."""
    _create_synthetic_package(pkg_name, directory)

    manifest_full = f"{pkg_name}.manifest"
    spec = importlib.util.spec_from_file_location(
        manifest_full,
        manifest_path,
        submodule_search_locations=None,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {manifest_path}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = pkg_name
    sys.modules[manifest_full] = module
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit):
        sys.modules.pop(manifest_full, None)
        raise
    except KeyboardInterrupt:
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


def _cleanup_package(pkg_name: str) -> None:
    """Remove the synthetic package and every descendant from sys.modules."""
    prefix = f"{pkg_name}."
    to_remove = [key for key in list(sys.modules) if key == pkg_name or key.startswith(prefix)]
    for key in to_remove:
        sys.modules.pop(key, None)


__all__ = ["PACKAGE_PREFIX", "load_plugin"]
