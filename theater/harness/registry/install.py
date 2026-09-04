"""Install orchestration and broken-plugin handling.

``install`` rebuilds the registry from the shipped and local plugin
directories via :func:`theater.harness.loading.scan`. Local beats shipped.
A broken shipped plugin raises; a broken local plugin is skipped and listed
as broken by ``theater harnesses``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from theater import paths
from theater.config import Config, ConfigError
from theater.constants.core import HARNESS_NAME
from theater.harness import builtin, loading
from theater.harness.loading.models import LOCAL, SHIPPED, LoadedPlugin, PluginError
from theater.harness.registry import (
    _ALIASES,
    _BINARIES,
    _BROKEN,
    _OBSERVATION_KEYS,
    _PLUGINS,
    HARNESSES,
)
from theater.harness.registry.claims import (
    claim_alias,
    claim_binary,
    claim_name,
    claim_observation_keys,
    release_claims,
)
from theater.mcp_plugins import registry as mcp_registry
from theater.plugins.namespace import PluginNameReservation

logger = logging.getLogger("theater.harness")


def install(
    config: Config,
    *,
    local_dir: Path | None = None,
    shipped_dir: Path | None = None,
    mcp_local_dir: Path | None = None,
    mcp_shipped_dir: Path | None = None,
) -> list[str]:
    """Rebuild the registry from the shipped and local plugin directories.

    Rebuilt rather than extended, so calling it twice is the same as calling
    it once. Local beats shipped. A broken shipped plugin raises; a broken
    local plugin is skipped with a warning and listed as broken.
    Returns the registered names, sorted.
    """
    disabled = set(config.harness.disabled)
    shipped_root = shipped_dir if shipped_dir is not None else builtin.plugin_dir()
    local_root = local_dir if local_dir is not None else paths.harnesses_dir()

    HARNESSES.clear()
    _ALIASES.clear()
    _PLUGINS.clear()
    _BINARIES.clear()
    _OBSERVATION_KEYS.clear()
    _BROKEN.clear()

    mcp_registry.install(
        config,
        local_dir=mcp_local_dir,
        shipped_dir=mcp_shipped_dir,
        reserved_harnesses=_reserved_harness_names(shipped_root, local_root),
    )

    shipped = loading.scan(
        shipped_root,
        source=SHIPPED,
        skip=disabled,
    )
    local = loading.scan(
        local_root,
        source=LOCAL,
        skip=disabled,
    )

    for found in [*shipped, *local]:
        if found.name in disabled:
            continue
        if found.harness is None:
            _reject(found, config)
            continue
        previous = _PLUGINS.get(found.name)
        if previous is not None and previous.source == found.source:
            raise ConfigError(
                f"two {found.source} plugins both define the harness "
                f"{found.name!r}: {previous.path} and {found.path}"
            )
        if previous is not None:
            logger.info(
                "%s plugin %s overrides the %s %s",
                found.source,
                found.path,
                previous.source,
                previous.path,
            )
            if previous.harness is not None:
                release_claims(previous.harness, previous.name)
        claim_name(found.name, str(found.path))
        HARNESSES[found.name] = found.harness
        _PLUGINS[found.name] = found
        for alias in found.harness.aliases:
            claim_alias(alias, found.name, str(found.path))
        claim_binary(found.harness.binary, found.name, str(found.path))
        for extra in found.harness.binaries:
            claim_binary(extra, found.name, str(found.path))
        claim_observation_keys(found.harness.binary, found.name, str(found.path))
        for extra in found.harness.binaries:
            claim_observation_keys(extra, found.name, str(found.path))

    return sorted(HARNESSES)


def _reserved_harness_names(
    shipped_dir: Path,
    local_dir: Path,
) -> tuple[PluginNameReservation, ...]:
    """Reserve every discovered canonical harness name before either kind imports."""
    discovered = [
        *loading.discover(shipped_dir, source=SHIPPED),
        *loading.discover(local_dir, source=LOCAL),
    ]
    return tuple(
        PluginNameReservation(
            kind="harness",
            name=plugin.name,
            path=plugin.path,
            source=plugin.source,
        )
        for plugin in discovered
        if HARNESS_NAME.fullmatch(plugin.name) is not None
    )


def _reject(found: LoadedPlugin, config: Config) -> None:
    """A plugin that would not load: fatal if we shipped it, logged if not."""
    if found.source == SHIPPED:
        where = config.path or paths.config_path()
        raise PluginError(
            f"{found.error}\n  This is an adapter Theater ships, so this is a "
            f"bug. To start without it, put\n    [harness]\n    disabled = "
            f'["{found.name}"]\n  in {where}'
        )
    logger.warning("skipping harness plugin %s: %s", found.path, found.error)
    _BROKEN.append(found)
