"""Local, manifest-derived diagnostics shared by every plugin kind."""

from __future__ import annotations

import re
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

_RESERVED_MCP_NAMES = frozenset({HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME})
_UNSAFE_EXCEPTION = re.compile(r": (?:[A-Za-z_][A-Za-z0-9_.]*Error|Exception|SystemExit)\b")


def describe(
    config: Config,
    *,
    harness_local_dir: Path | None = None,
    harness_shipped_dir: Path | None = None,
    mcp_local_dir: Path | None = None,
    mcp_shipped_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Inspect package manifests locally without mutating either live registry."""
    harnesses = [
        *harness_discovery.discover(
            harness_shipped_dir or harness_builtin.plugin_dir(), source=SHIPPED
        ),
        *harness_discovery.discover(harness_local_dir or paths.harnesses_dir(), source=LOCAL),
    ]
    mcps = [
        *discover_mcp(mcp_shipped_dir or mcp_builtin.plugin_dir(), source=SHIPPED),
        *discover_mcp(mcp_local_dir or paths.mcp_servers_dir(), source=LOCAL),
    ]
    harness_names = {plugin.name for plugin in harnesses if _canonical(plugin.name)}
    mcp_names = {plugin.name for plugin in mcps if _canonical(plugin.name)}
    conflicts = harness_names & mcp_names
    disabled_harnesses = set(config.harness.disabled)
    rows = [
        _harness_row(plugin, disabled=plugin.name in disabled_harnesses, conflicts=conflicts)
        for plugin in harnesses
    ]
    rows.extend(_mcp_rows(mcps, config, conflicts))
    return sorted(rows, key=lambda row: (row["kind"], row["name"], row["source"] or ""))


def _harness_row(plugin: LoadedPlugin, *, disabled: bool, conflicts: set[str]) -> dict[str, Any]:
    row = _base_row("harness", plugin, enabled=not disabled)
    if plugin.name in conflicts:
        return _broken(row, _cross_kind_error(plugin.name))
    if disabled:
        row["state"] = "disabled"
        return row
    loaded = harness_importer.load_plugin(plugin)
    if loaded.error is not None or loaded.manifest is None:
        return _broken(row, _safe_error(loaded.error))
    manifest = loaded.manifest
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
    selected = _selected(plugins)
    rows: list[dict[str, Any]] = []
    for plugin in plugins:
        selected_plugin = selected.get(plugin.name)
        is_selected = selected_plugin == plugin
        row = _base_row("mcp_server", plugin, enabled=plugin.name in enabled)
        if plugin.name in conflicts:
            rows.append(_broken(row, _cross_kind_error(plugin.name)))
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
            rows.append(_broken(row, _safe_error(plugin.error)))
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
    loaded = load_mcp(plugin)
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
    )
    return row


def _selected(plugins: Iterable[LoadedMcpPlugin]) -> dict[str, LoadedMcpPlugin]:
    by_name: dict[str, dict[str, list[LoadedMcpPlugin]]] = {
        SHIPPED: defaultdict(list),
        LOCAL: defaultdict(list),
    }
    for plugin in plugins:
        by_name[plugin.source][plugin.name].append(plugin)
    selected: dict[str, LoadedMcpPlugin] = {}
    for source in (SHIPPED, LOCAL):
        for name, group in by_name[source].items():
            if len(group) == 1:
                selected[name] = group[0]
    return selected


def _base_row(
    kind: str, plugin: LoadedPlugin | LoadedMcpPlugin, *, enabled: bool
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


def _canonical(name: str) -> bool:
    return HARNESS_NAME.fullmatch(name) is not None


def _cross_kind_error(name: str) -> str:
    return f"canonical plugin name {name!r} is claimed by both a harness and an MCP-server package"


def _safe_error(error: str | None, *, fallback: str = "manifest could not be loaded") -> str:
    """Keep arbitrary plugin exception values and configured values out of diagnostics."""
    if not error:
        return fallback
    if _UNSAFE_EXCEPTION.search(error) or "failed:" in error:
        return fallback
    return " ".join("".join(char if char.isprintable() else " " for char in error).split())


__all__ = ["describe"]
