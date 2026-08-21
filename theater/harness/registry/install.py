"""Install orchestration and broken-plugin handling.

``install`` rebuilds the registry from the shipped and local plugin
directories. Local beats shipped. A broken shipped plugin raises; a broken
local plugin is skipped and listed as broken by ``theater harnesses``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from theater import paths
from theater.config import Config, ConfigError
from theater.harness import builtin, plugins
from theater.harness.plugins import Plugin, PluginError
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

logger = logging.getLogger("theater.harness")


def install(
    config: Config,
    *,
    local_dir: Path | None = None,
    shipped_dir: Path | None = None,
) -> list[str]:
    """Rebuild the registry from the shipped and local plugin directories.

    Rebuilt rather than extended, so calling it twice is the same as calling
    it once. Local beats shipped. A broken shipped plugin raises; a broken
    local plugin is skipped with a warning and listed as broken.
    Returns the registered names, sorted.
    """
    disabled = set(config.harness.disabled)

    HARNESSES.clear()
    _ALIASES.clear()
    _PLUGINS.clear()
    _BINARIES.clear()
    _OBSERVATION_KEYS.clear()
    _BROKEN.clear()

    shipped = plugins.scan(
        shipped_dir if shipped_dir is not None else builtin.plugin_dir(),
        source=plugins.SHIPPED,
        skip=disabled,
    )
    local = plugins.scan(
        local_dir if local_dir is not None else paths.harnesses_dir(),
        source=plugins.LOCAL,
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


def _reject(found: Plugin, config: Config) -> None:
    """A plugin that would not load: fatal if we shipped it, logged if not."""
    if found.source == plugins.SHIPPED:
        where = config.path or paths.config_path()
        raise PluginError(
            f"{found.error}\n  This is an adapter Theater ships, so this is a "
            f"bug. To start without it, put\n    [harness]\n    disabled = "
            f'["{found.name}"]\n  in {where}'
        )
    logger.warning("skipping harness plugin %s: %s", found.path, found.error)
    _BROKEN.append(found)
