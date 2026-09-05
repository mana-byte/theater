"""Package discovery and isolated import for MCP-server manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from theater.config import ConfigError
from theater.constants.core import HARNESS_NAME
from theater.mcp_plugins.contracts import McpServerManifest
from theater.mcp_plugins.validation import McpManifestValidationError, validate_manifest
from theater.plugins.loading import (
    LOCAL,
    MANIFEST_FILENAME,
    SHIPPED,
    discover_packages,
)
from theater.plugins.loading import (
    cleanup_package as _cleanup_shared_package,
)
from theater.plugins.loading import (
    import_manifest as _import_shared_manifest,
)
from theater.plugins.loading import (
    synthetic_package_name as _shared_synthetic_package_name,
)
from theater.skills.loader import load_declared_skills
from theater.skills.models import Skill, SkillValidationError

PACKAGE_PREFIX = "theater_mcp_server_pkg_"


class McpPluginError(ConfigError):
    """A shipped MCP-server package cannot be used safely."""


@dataclass(frozen=True, slots=True)
class LoadedMcpPlugin:
    """One discovered MCP-server package, imported only when it is enabled."""

    path: Path
    source: str
    name: str
    manifest: McpServerManifest | None = None
    skills: tuple[Skill, ...] = ()
    error: str | None = None


def discover(root: Path, *, source: str) -> list[LoadedMcpPlugin]:
    """Discover every MCP-server package directory without importing its manifest."""
    candidates = discover_packages(
        root,
        source=source,
        kind="MCP server",
        name_pattern=HARNESS_NAME,
    )
    return [
        LoadedMcpPlugin(
            path=candidate.path,
            source=candidate.source,
            name=candidate.name,
            error=candidate.error,
        )
        for candidate in candidates
    ]


def load_plugin(plugin: LoadedMcpPlugin) -> LoadedMcpPlugin:
    """Import and statically validate one already discovered MCP-server package."""
    if plugin.error is not None:
        return plugin
    manifest_path = plugin.path / MANIFEST_FILENAME
    package_name = _synthetic_package_name(plugin.path, plugin.source)
    try:
        module, _ = _import_shared_manifest(plugin.path, plugin.source, prefix=PACKAGE_PREFIX)
    except KeyboardInterrupt:
        _cleanup_package(package_name)
        raise
    except (Exception, SystemExit) as exc:
        _cleanup_package(package_name)
        return _broken(plugin, f"{manifest_path}: {exc!r}")

    manifest = getattr(module, "MANIFEST", None)
    if manifest is None:
        _cleanup_package(package_name)
        return _broken(
            plugin,
            f"{manifest_path}: defines no MANIFEST. A plugin must end with "
            "`MANIFEST = McpServerManifest(...)`",
        )
    if isinstance(manifest, type):
        _cleanup_package(package_name)
        return _broken(
            plugin,
            f"{manifest_path}: MANIFEST is the class {manifest.__name__}, not an instance. "
            f"Use `MANIFEST = {manifest.__name__}(...)`",
        )
    if not isinstance(manifest, McpServerManifest):
        guidance = ""
        if type(manifest).__name__ == "HarnessManifest":
            guidance = (
                " Harness packages belong in $THEATER_HOME/plugins/<name>/manifest.py; "
                "a package may export only one manifest kind"
            )
        _cleanup_package(package_name)
        return _broken(
            plugin,
            f"{manifest_path}: MANIFEST is a {type(manifest).__name__}, which is not a "
            f"McpServerManifest.{guidance}",
        )
    try:
        validate_manifest(plugin.name, manifest)
    except KeyboardInterrupt:
        _cleanup_package(package_name)
        raise
    except McpManifestValidationError as exc:
        _cleanup_package(package_name)
        return _broken(plugin, f"{manifest_path}: {exc}")
    except (Exception, SystemExit) as exc:
        _cleanup_package(package_name)
        return _broken(plugin, f"{manifest_path}: validation failed: {exc!r}")
    try:
        skills = load_declared_skills(
            plugin.path / "skills",
            manifest.skills,
            provider=plugin.name,
        )
    except SkillValidationError as exc:
        _cleanup_package(package_name)
        return _broken(plugin, f"{plugin.path}: {exc}")
    return LoadedMcpPlugin(
        path=plugin.path,
        source=plugin.source,
        name=plugin.name,
        manifest=manifest,
        skills=skills,
    )


def scan(directory: Path, *, source: str) -> list[LoadedMcpPlugin]:
    """Discover and import every MCP-server package in deterministic name order."""
    return [load_plugin(plugin) for plugin in discover(directory, source=source)]


def _synthetic_package_name(directory: Path, source: str) -> str:
    return _shared_synthetic_package_name(directory, source, prefix=PACKAGE_PREFIX)


def _cleanup_package(package_name: str) -> None:
    _cleanup_shared_package(package_name)


def _broken(plugin: LoadedMcpPlugin, error: str) -> LoadedMcpPlugin:
    return LoadedMcpPlugin(
        path=plugin.path,
        source=plugin.source,
        name=plugin.name,
        error=error,
    )


__all__ = [
    "LOCAL",
    "MANIFEST_FILENAME",
    "PACKAGE_PREFIX",
    "SHIPPED",
    "LoadedMcpPlugin",
    "McpPluginError",
    "discover",
    "load_plugin",
    "scan",
]
