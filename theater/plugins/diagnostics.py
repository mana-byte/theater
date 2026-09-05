"""Local, manifest-derived diagnostics shared by every plugin kind."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from theater import paths
from theater.config import Config
from theater.constants.core import HARNESS_NAME
from theater.constants.harness import HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME
from theater.harness import builtin as harness_builtin
from theater.harness.loading import discovery as harness_discovery
from theater.harness.loading import importer as harness_importer
from theater.harness.loading.models import LoadedPlugin
from theater.mcp_plugins import builtin as mcp_builtin
from theater.mcp_plugins.compiler import compile_manifest
from theater.mcp_plugins.loading import LOCAL, SHIPPED, LoadedMcpPlugin
from theater.mcp_plugins.loading import discover as discover_mcp
from theater.mcp_plugins.loading import load_plugin as load_mcp
from theater.plugins.catalog import RejectedPlugin, SkippedPlugin
from theater.plugins.catalog import scan as scan_user_plugins

_RESERVED_MCP_NAMES = frozenset({HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME})


def describe(
    config: Config,
    *,
    local_dir: Path | None = None,
    harness_shipped_dir: Path | None = None,
    mcp_shipped_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Inspect package manifests locally without mutating either live registry."""
    user = scan_user_plugins(
        local_dir or paths.plugins_dir(),
        skip=set(config.harness.disabled) - set(config.mcp.enabled),
    )
    harnesses = [
        *harness_discovery.discover(
            harness_shipped_dir or harness_builtin.plugin_dir(), source=SHIPPED
        ),
        *user.harnesses,
    ]
    mcps = [
        *discover_mcp(mcp_shipped_dir or mcp_builtin.plugin_dir(), source=SHIPPED),
        *user.mcp_servers,
    ]
    harness_names = {plugin.name for plugin in harnesses if _canonical(plugin.name)}
    mcp_names = {plugin.name for plugin in mcps if _canonical(plugin.name)}
    conflicts = harness_names & mcp_names
    disabled_harnesses = set(config.harness.disabled)
    rows = _harness_rows(harnesses, disabled=disabled_harnesses, conflicts=conflicts)
    rows.extend(_mcp_rows(mcps, config, conflicts))
    rows.extend(_rejected_rows(user.rejected, config))
    rows.extend(_skipped_rows(user.skipped))
    return sorted(rows, key=lambda row: (row["kind"], row["name"], row["source"] or ""))


def _harness_rows(
    plugins: list[LoadedPlugin], *, disabled: set[str], conflicts: set[str]
) -> list[dict[str, Any]]:
    loaded: dict[Path, LoadedPlugin] = {}
    local_valid: set[str] = set()
    for plugin in plugins:
        if plugin.name in disabled or plugin.name in conflicts:
            continue
        result = (
            plugin
            if plugin.harness is not None and plugin.manifest is not None
            else harness_importer.load_plugin(plugin)
        )
        loaded[plugin.path] = result
        if plugin.source == LOCAL and result.manifest is not None and result.error is None:
            local_valid.add(plugin.name)

    rows: list[dict[str, Any]] = []
    for plugin in plugins:
        row = _base_row("harness", plugin, enabled=plugin.name not in disabled)
        if plugin.name in conflicts:
            rows.append(_conflict(row, plugin.name))
        elif plugin.name in disabled:
            row["state"] = "disabled"
            rows.append(row)
        else:
            result = loaded[plugin.path]
            if plugin.error is not None:
                rows.append(_broken(row, _structural_error(plugin.error)))
            elif result.error is not None or result.manifest is None:
                rows.append(_broken(row, _safe_error(result.error)))
            elif plugin.source == SHIPPED and plugin.name in local_valid:
                row["state"] = "overridden"
                row["error"] = "a local harness package with this canonical name takes precedence"
                rows.append(row)
            else:
                rows.append(_loaded_harness_row(row, result))
    return rows


def _loaded_harness_row(row: dict[str, Any], plugin: LoadedPlugin) -> dict[str, Any]:
    assert plugin.manifest is not None
    manifest = plugin.manifest
    row.update(
        state="loaded",
        manifest_api_version=manifest.api_version,
        binary=manifest.binary,
        approvals=list(manifest.launch.approvals),
    )
    return row


def _mcp_rows(
    plugins: list[LoadedMcpPlugin], config: Config, conflicts: set[str]
) -> list[dict[str, Any]]:
    enabled = set(config.mcp.enabled)
    selected, duplicates = _selected(plugins)
    rows: list[dict[str, Any]] = []
    for plugin in plugins:
        selected_plugin = selected.get(plugin.name)
        is_selected = selected_plugin == plugin
        row = _base_row("mcp_server", plugin, enabled=plugin.name in enabled)
        if plugin.name in conflicts:
            rows.append(_conflict(row, plugin.name))
        elif plugin in duplicates:
            row["state"] = "duplicate"
            row["error"] = (
                f"multiple {plugin.source} MCP-server packages reserve canonical name "
                f"{plugin.name!r}; none is enabled"
            )
            rows.append(row)
        elif not is_selected:
            row["state"] = "overridden"
            row["error"] = "a local MCP-server package with this canonical name takes precedence"
            rows.append(row)
        elif plugin.name not in enabled:
            row["state"] = "disabled"
            rows.append(row)
        elif plugin.name in _RESERVED_MCP_NAMES:
            rows.append(_broken(row, f"MCP server name {plugin.name!r} is reserved by Theater"))
        elif plugin.error is not None:
            rows.append(_broken(row, _structural_error(plugin.error)))
        else:
            rows.append(_loaded_mcp_row(row, plugin, config))
    found = {plugin.name for plugin in plugins if _canonical(plugin.name)}
    for name in sorted(enabled - found):
        rows.append(
            {
                "kind": "mcp_server",
                "name": name,
                "source": None,
                "enabled": True,
                "state": "missing",
                "path": None,
                "manifest_path": None,
                "error": f"enabled MCP-server package {name!r} was not found",
            }
        )
    return rows


def _loaded_mcp_row(row: dict[str, Any], plugin: LoadedMcpPlugin, config: Config) -> dict[str, Any]:
    loaded = plugin if plugin.manifest is not None else load_mcp(plugin)
    if loaded.error is not None or loaded.manifest is None:
        return _broken(row, _safe_error(loaded.error))
    try:
        compiled = compile_manifest(
            plugin.name, loaded.manifest, config.mcp.plugins.get(plugin.name)
        )
    except Exception as exc:
        return _broken(
            row,
            _safe_error(str(exc), fallback="enabled configuration could not be used"),
        )
    row.update(
        state="loaded",
        manifest_api_version=loaded.manifest.api_version,
        description=compiled.description,
        capabilities=sorted(capability.value for capability in compiled.capabilities),
        skills=[skill.name for skill in loaded.skills],
    )
    return row


def _rejected_rows(plugins: Iterable[RejectedPlugin], config: Config) -> list[dict[str, Any]]:
    enabled = set(config.mcp.enabled)
    rows = []
    for plugin in plugins:
        row = _base_row("plugin", plugin, enabled=plugin.name in enabled)
        rows.append(_broken(row, _safe_error(plugin.error)))
    return rows


def _skipped_rows(plugins: Iterable[SkippedPlugin]) -> list[dict[str, Any]]:
    rows = []
    for plugin in plugins:
        row = _base_row("plugin", plugin, enabled=False)
        row["state"] = "disabled"
        rows.append(row)
    return rows


def _selected(
    plugins: Iterable[LoadedMcpPlugin],
) -> tuple[dict[str, LoadedMcpPlugin], tuple[LoadedMcpPlugin, ...]]:
    by_name: dict[str, dict[str, list[LoadedMcpPlugin]]] = {
        SHIPPED: defaultdict(list),
        LOCAL: defaultdict(list),
    }
    for plugin in plugins:
        by_name[plugin.source][plugin.name].append(plugin)
    selected: dict[str, LoadedMcpPlugin] = {}
    duplicates: list[LoadedMcpPlugin] = []
    for source in (SHIPPED, LOCAL):
        for name, group in by_name[source].items():
            if len(group) == 1:
                selected[name] = group[0]
            else:
                duplicates.extend(group)
    return selected, tuple(duplicates)


def _base_row(
    kind: str,
    plugin: LoadedPlugin | LoadedMcpPlugin | RejectedPlugin | SkippedPlugin,
    *,
    enabled: bool,
) -> dict[str, Any]:
    manifest_path = plugin.path / "manifest.py" if plugin.path.is_dir() else plugin.path
    return {
        "kind": kind,
        "name": plugin.name,
        "source": plugin.source,
        "enabled": enabled,
        "state": "discovered",
        "path": str(plugin.path),
        "manifest_path": str(manifest_path),
        "error": None,
    }


def _broken(row: dict[str, Any], error: str) -> dict[str, Any]:
    row["state"] = "broken"
    row["error"] = error
    return row


def _conflict(row: dict[str, Any], name: str) -> dict[str, Any]:
    row["state"] = "conflict"
    row["error"] = _cross_kind_error(name)
    return row


def _canonical(name: str) -> bool:
    return HARNESS_NAME.fullmatch(name) is not None


def _cross_kind_error(name: str) -> str:
    return (
        f"canonical plugin name {name!r} is claimed by both a harness and an MCP-server "
        "package; this cross-kind conflict prevents daemon startup. Rename or remove one package "
        "(including a disabled package) so their canonical names differ"
    )


def _safe_error(error: str | None, *, fallback: str = "manifest could not be loaded") -> str:
    """Expose only diagnostics Theater generated from package structure or manifest kind."""
    if not error:
        return fallback
    if "MANIFEST is a HarnessManifest" in error:
        return "manifest exports HarnessManifest; MCP-server packages require McpServerManifest"
    if "MANIFEST is a McpServerManifest" in error:
        return "manifest exports McpServerManifest; harness packages require HarnessManifest"
    if "MANIFEST is the class" in error:
        return "manifest exports a class rather than a manifest instance"
    if "registered skill" in error or "MCP-plugin skill root" in error:
        return "a declared MCP-plugin skill package is invalid or missing"
    return fallback


def _structural_error(error: str) -> str:
    """Normalize a pre-import discovery error emitted by Theater's package scanner."""
    return " ".join("".join(char if char.isprintable() else " " for char in error).split())


__all__ = ["describe"]
