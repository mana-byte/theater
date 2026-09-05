"""Load and classify packages from the shared user plugin root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from theater.constants.core import HARNESS_NAME
from theater.harness.contracts.manifest import HarnessManifest
from theater.harness.loading.models import LoadedPlugin
from theater.harness.manifests.compiler import compile_manifest as compile_harness
from theater.harness.manifests.validation import ManifestValidationError
from theater.mcp_plugins.contracts import McpServerManifest
from theater.mcp_plugins.loading import LoadedMcpPlugin
from theater.mcp_plugins.validation import McpManifestValidationError, validate_manifest
from theater.plugins.loading import (
    LOCAL,
    MANIFEST_FILENAME,
    cleanup_package,
    discover_packages,
    import_manifest,
    synthetic_package_name,
)
from theater.skills.loader import load_declared_skills
from theater.skills.models import SkillValidationError

_PACKAGE_PREFIX = "theater_user_plugin_pkg_"


@dataclass(frozen=True, slots=True)
class RejectedPlugin:
    path: Path
    source: str
    name: str
    error: str


@dataclass(frozen=True, slots=True)
class SkippedPlugin:
    path: Path
    source: str
    name: str


@dataclass(frozen=True, slots=True)
class UserPluginCatalog:
    harnesses: tuple[LoadedPlugin, ...]
    mcp_servers: tuple[LoadedMcpPlugin, ...]
    rejected: tuple[RejectedPlugin, ...]
    skipped: tuple[SkippedPlugin, ...]


def scan(root: Path, *, skip: set[str] | frozenset[str] = frozenset()) -> UserPluginCatalog:
    harnesses: list[LoadedPlugin] = []
    mcp_servers: list[LoadedMcpPlugin] = []
    rejected: list[RejectedPlugin] = []
    skipped: list[SkippedPlugin] = []
    candidates = discover_packages(
        root,
        source=LOCAL,
        kind="plugin",
        name_pattern=HARNESS_NAME,
    )
    for candidate in candidates:
        if candidate.name in skip:
            skipped.append(SkippedPlugin(candidate.path, candidate.source, candidate.name))
            continue
        if candidate.error is not None:
            rejected.append(
                RejectedPlugin(candidate.path, candidate.source, candidate.name, candidate.error)
            )
            continue
        loaded = _load(candidate.path, candidate.name, candidate.source)
        if isinstance(loaded, LoadedPlugin):
            harnesses.append(loaded)
        elif isinstance(loaded, LoadedMcpPlugin):
            mcp_servers.append(loaded)
        else:
            rejected.append(loaded)
    return UserPluginCatalog(tuple(harnesses), tuple(mcp_servers), tuple(rejected), tuple(skipped))


def _load(path: Path, name: str, source: str) -> LoadedPlugin | LoadedMcpPlugin | RejectedPlugin:
    manifest_path = path / MANIFEST_FILENAME
    package_name = synthetic_package_name(path, source, prefix=_PACKAGE_PREFIX)
    try:
        module, _ = import_manifest(path, source, prefix=_PACKAGE_PREFIX)
    except KeyboardInterrupt:
        cleanup_package(package_name)
        raise
    except (Exception, SystemExit) as exc:
        cleanup_package(package_name)
        return RejectedPlugin(path, source, name, f"{manifest_path}: {exc!r}")

    manifest = getattr(module, "MANIFEST", None)
    is_harness = isinstance(manifest, HarnessManifest)
    is_mcp = isinstance(manifest, McpServerManifest)
    if is_harness == is_mcp:
        cleanup_package(package_name)
        if isinstance(manifest, type):
            detail = f"MANIFEST is the class {manifest.__name__}, not an instance"
        else:
            detail = "MANIFEST must be exactly one HarnessManifest or McpServerManifest instance"
        return RejectedPlugin(path, source, name, f"{manifest_path}: {detail}")

    if is_harness:
        assert isinstance(manifest, HarnessManifest)
        try:
            harness = compile_harness(name, manifest)
        except KeyboardInterrupt:
            cleanup_package(package_name)
            raise
        except ManifestValidationError as exc:
            cleanup_package(package_name)
            return RejectedPlugin(path, source, name, f"{manifest_path}: {exc}")
        except (Exception, SystemExit) as exc:
            cleanup_package(package_name)
            return RejectedPlugin(path, source, name, f"{manifest_path}: {exc!r}")
        return LoadedPlugin(
            path=path,
            source=source,
            name=name,
            harness=harness,
            manifest=manifest,
        )

    assert isinstance(manifest, McpServerManifest)
    try:
        validate_manifest(name, manifest)
        skills = load_declared_skills(path / "skills", manifest.skills, provider=name)
    except KeyboardInterrupt:
        cleanup_package(package_name)
        raise
    except (McpManifestValidationError, SkillValidationError) as exc:
        cleanup_package(package_name)
        return RejectedPlugin(path, source, name, f"{manifest_path}: {exc}")
    except (Exception, SystemExit) as exc:
        cleanup_package(package_name)
        return RejectedPlugin(path, source, name, f"{manifest_path}: {exc!r}")
    return LoadedMcpPlugin(
        path=path,
        source=source,
        name=name,
        manifest=manifest,
        skills=skills,
    )


__all__ = ["RejectedPlugin", "SkippedPlugin", "UserPluginCatalog", "scan"]
