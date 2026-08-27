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
from theater.harness import builtin, loading, plugins
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

    shipped = _scan(
        shipped_dir if shipped_dir is not None else builtin.plugin_dir(),
        source=plugins.SHIPPED,
        disabled=disabled,
    )
    local = _scan(
        local_dir if local_dir is not None else paths.harnesses_dir(),
        source=plugins.LOCAL,
        disabled=disabled,
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


def _scan(directory: Path, *, source: str, disabled: set[str]) -> list[Plugin]:
    """Package manifests first, then not-yet-migrated single files.

    Transitional for the Phase 4 built-in migration: a name available as a
    package shadows the legacy file of the same name, and the package
    loader's legacy-file diagnostics stay silent while the old scanner still
    executes those files. Both halves collapse to the package loader once
    every shipped plugin is a directory.
    """
    packages = [
        found
        for found in loading.scan(directory, source=source, skip=disabled)
        if not (found.path.is_file() and found.path.suffix == ".py")
    ]
    migrated = {found.name for found in packages}
    files = [
        found
        for found in plugins.scan(directory, source=source, skip=disabled)
        if found.name not in migrated
    ]
    return [*packages, *files]


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
