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

from pathlib import Path

from theater.harness.contracts.manifest import HarnessManifest
from theater.harness.loading.discovery import MANIFEST_FILENAME
from theater.harness.loading.models import LoadedPlugin
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.validation import ManifestValidationError
from theater.plugins.loading import (
    cleanup_package as _cleanup_shared_package,
)
from theater.plugins.loading import (
    import_manifest as _import_shared_manifest,
)
from theater.plugins.loading import (
    synthetic_package_name as _shared_synthetic_package_name,
)

PACKAGE_PREFIX = "theater_harness_pkg_"


def _synthetic_package_name(directory: Path, source: str) -> str:
    return _shared_synthetic_package_name(directory, source, prefix=PACKAGE_PREFIX)


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

    try:
        manifest_module, _ = _import_shared_manifest(
            directory,
            plugin.source,
            prefix=PACKAGE_PREFIX,
        )
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
        guidance = ""
        if type(manifest).__name__ == "McpServerManifest":
            guidance = (
                " MCP-server packages belong in $THEATER_HOME/plugins/<name>/manifest.py; "
                "a package may export only one manifest kind"
            )
        _cleanup_package(pkg_name)
        return LoadedPlugin(
            path=directory,
            source=plugin.source,
            name=name,
            error=f"{manifest_path}: MANIFEST is a {type(manifest).__name__}, "
            f"which is not a HarnessManifest — see docs/harness-plugins.md.{guidance}",
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
        manifest=manifest,
    )


def _cleanup_package(pkg_name: str) -> None:
    """Remove the synthetic package and every descendant from sys.modules."""
    _cleanup_shared_package(pkg_name)


__all__ = ["PACKAGE_PREFIX", "load_plugin"]
