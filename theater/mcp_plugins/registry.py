"""Independent MCP-server plugin registry and shared-name catalog checks."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from theater import paths
from theater.config import Config, ConfigError
from theater.constants.core import HARNESS_NAME
from theater.constants.harness import HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME
from theater.mcp_plugins import builtin
from theater.mcp_plugins.compiler import compile_manifest
from theater.mcp_plugins.contracts import CompiledMcpPlugin
from theater.mcp_plugins.loading import (
    LOCAL,
    SHIPPED,
    LoadedMcpPlugin,
    McpPluginError,
    discover,
    load_plugin,
)
from theater.plugins.namespace import (
    NamespaceCollision,
    PluginNameReservation,
    reject_cross_kind_collisions,
)

logger = logging.getLogger("theater.mcp_plugins")

_RESERVED_SERVER_NAMES = frozenset({HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME})

#: The separate live registry for canonical configured MCP plugins.
MCP_SERVERS: dict[str, CompiledMcpPlugin] = {}

#: Selected package result by canonical name, for diagnostics and future rendering.
_PLUGINS: dict[str, LoadedMcpPlugin] = {}

#: Omitted local packages and enabled-config problems, kept rather than made fatal.
_DIAGNOSTICS: list[McpPluginDiagnostic] = []


@dataclass(frozen=True, slots=True)
class McpPluginDiagnostic:
    """One safe, non-fatal omission from the MCP-server registry."""

    name: str
    error: str
    path: Path | None = None
    source: str | None = None
    requested: bool = False


@dataclass(frozen=True, slots=True)
class McpPluginCatalog:
    """An immutable point-in-time view of the independent MCP-server registry."""

    _servers: Mapping[str, CompiledMcpPlugin]
    diagnostics: tuple[McpPluginDiagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_servers",
            MappingProxyType({name: self._servers[name] for name in sorted(self._servers)}),
        )
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    @property
    def servers(self) -> tuple[CompiledMcpPlugin, ...]:
        """Enabled, validated MCP plugins in canonical name order."""
        return tuple(self._servers.values())

    def get(self, name: str) -> CompiledMcpPlugin:
        """Return one compiled MCP plugin, or raise a clear lookup error."""
        try:
            return self._servers[name]
        except KeyError as exc:
            raise UnknownMcpServer(f"unknown MCP server plugin {name!r}") from exc


class UnknownMcpServer(LookupError):
    """No enabled, valid MCP-server plugin has the requested canonical name."""


def install(
    config: Config,
    *,
    local_dir: Path | None = None,
    shipped_dir: Path | None = None,
    reserved_harnesses: Iterable[PluginNameReservation] | Mapping[str, Path] = (),
) -> list[str]:
    """Rebuild the MCP-server registry without making local failures fatal.

    Every directory is discovered and reserves its canonical name. Only names
    listed by ``[mcp].enabled`` are imported, validated, and configured.
    """
    MCP_SERVERS.clear()
    _PLUGINS.clear()
    _DIAGNOSTICS.clear()

    shipped_root = shipped_dir if shipped_dir is not None else builtin.plugin_dir()
    local_root = local_dir if local_dir is not None else paths.mcp_servers_dir()
    shipped = discover(shipped_root, source=SHIPPED)
    local = discover(local_root, source=LOCAL)
    _check_namespace(reserved_harnesses, [*shipped, *local])

    enabled = set(config.mcp.enabled)
    selected, duplicates = _select_packages(shipped, local)
    for duplicate in duplicates:
        _append_diagnostic(
            McpPluginDiagnostic(
                name=duplicate.name,
                path=duplicate.path,
                source=duplicate.source,
                requested=duplicate.name in enabled,
                error=(
                    f"multiple {duplicate.source} MCP-server packages reserve canonical name "
                    f"{duplicate.name!r}; none is enabled"
                ),
            )
        )

    known_names = {
        plugin.name
        for plugin in [*shipped, *local]
        if HARNESS_NAME.fullmatch(plugin.name) is not None
    }
    for name in sorted(enabled - known_names):
        _append_diagnostic(
            McpPluginDiagnostic(
                name=name,
                error=f"enabled MCP-server package {name!r} was not found",
                requested=True,
            )
        )

    for name in sorted(selected):
        plugin = selected[name]
        if plugin.error is not None:
            _reject_shipped(plugin, enabled=name in enabled)
            _append_diagnostic(
                McpPluginDiagnostic(
                    name=name,
                    path=plugin.path,
                    source=plugin.source,
                    error=plugin.error,
                    requested=name in enabled,
                )
            )
            continue
        if name not in enabled:
            continue
        loaded = load_plugin(plugin)
        _PLUGINS[name] = loaded
        if loaded.error is not None or loaded.manifest is None:
            _reject_shipped(loaded, enabled=True)
            _append_diagnostic(
                McpPluginDiagnostic(
                    name=name,
                    path=loaded.path,
                    source=loaded.source,
                    error=loaded.error or "package did not produce a manifest",
                    requested=True,
                )
            )
            continue
        try:
            spec = compile_manifest(name, loaded.manifest, config.mcp.plugins.get(name))
        except Exception as exc:
            _append_diagnostic(
                McpPluginDiagnostic(
                    name=name,
                    path=loaded.path,
                    source=loaded.source,
                    error=f"enabled configuration could not be used: {exc}",
                    requested=True,
                )
            )
            continue
        if name in _RESERVED_SERVER_NAMES:
            _append_diagnostic(
                McpPluginDiagnostic(
                    name=name,
                    path=loaded.path,
                    source=loaded.source,
                    error=f"MCP server name {name!r} is reserved by Theater",
                    requested=True,
                )
            )
            continue
        MCP_SERVERS[name] = spec

    for diagnostic in _DIAGNOSTICS:
        logger.warning("omitting MCP-server plugin %s: %s", diagnostic.name, diagnostic.error)
    return sorted(MCP_SERVERS)


def catalog() -> McpPluginCatalog:
    """Return the current independent registry as immutable values."""
    return McpPluginCatalog(dict(MCP_SERVERS), tuple(_DIAGNOSTICS))


def get(name: str) -> CompiledMcpPlugin:
    """Look up one enabled compiled MCP plugin from the live registry."""
    return catalog().get(name)


def diagnostics() -> tuple[McpPluginDiagnostic, ...]:
    """Return retained non-fatal omission diagnostics."""
    return tuple(_DIAGNOSTICS)


def _select_packages(
    shipped: list[LoadedMcpPlugin], local: list[LoadedMcpPlugin]
) -> tuple[dict[str, LoadedMcpPlugin], tuple[LoadedMcpPlugin, ...]]:
    by_source: dict[str, dict[str, list[LoadedMcpPlugin]]] = {
        SHIPPED: defaultdict(list),
        LOCAL: defaultdict(list),
    }
    for plugin in shipped:
        by_source[SHIPPED][plugin.name].append(plugin)
    for plugin in local:
        by_source[LOCAL][plugin.name].append(plugin)

    duplicates: list[LoadedMcpPlugin] = []
    unique: dict[str, dict[str, LoadedMcpPlugin]] = {SHIPPED: {}, LOCAL: {}}
    for source, groups in by_source.items():
        for name, group in groups.items():
            if len(group) != 1:
                duplicates.extend(group)
                continue
            unique[source][name] = group[0]

    selected = dict(unique[SHIPPED])
    selected.update(unique[LOCAL])
    return selected, tuple(duplicates)


def _check_namespace(
    reserved_harnesses: Iterable[PluginNameReservation] | Mapping[str, Path],
    plugins: Iterable[LoadedMcpPlugin],
) -> None:
    if isinstance(reserved_harnesses, Mapping):
        harnesses = tuple(
            PluginNameReservation(kind="harness", name=name, path=path, source="unknown")
            for name, path in reserved_harnesses.items()
        )
    else:
        harnesses = tuple(reserved_harnesses)
    mcp = tuple(
        PluginNameReservation(
            kind="MCP server",
            name=plugin.name,
            path=plugin.path,
            source=plugin.source,
        )
        for plugin in plugins
        if HARNESS_NAME.fullmatch(plugin.name) is not None
    )
    try:
        reject_cross_kind_collisions(harnesses, mcp)
    except NamespaceCollision as exc:
        raise ConfigError(str(exc)) from exc


def _append_diagnostic(diagnostic: McpPluginDiagnostic) -> None:
    _DIAGNOSTICS.append(diagnostic)


def _reject_shipped(plugin: LoadedMcpPlugin, *, enabled: bool) -> None:
    """Fail closed for an enabled package Theater itself shipped."""
    if plugin.source != SHIPPED or not enabled:
        return
    raise McpPluginError(
        f"{plugin.error or 'MCP-server package did not produce a manifest'}\n"
        "  This is an MCP-server package Theater ships, so this is a bug. Disable it by "
        "removing its name from [mcp].enabled."
    )


__all__ = [
    "MCP_SERVERS",
    "McpPluginCatalog",
    "McpPluginDiagnostic",
    "UnknownMcpServer",
    "catalog",
    "diagnostics",
    "get",
    "install",
]
