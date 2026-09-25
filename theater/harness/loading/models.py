"""Result values and error types for the package manifest loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from theater.config import ConfigError
from theater.harness.contracts.harness import Harness
from theater.plugins.loading import LOCAL, SHIPPED

if TYPE_CHECKING:
    from theater.harness.contracts.manifest import HarnessManifest


class PluginError(ConfigError):
    """A plugin manifest that cannot be turned into a harness."""


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """One discovered plugin, loaded or not.

    Path and source make collision messages actionable; a broken plugin keeps its name so
    `[harness] disabled` can switch it off.
    """

    path: Path
    source: str
    name: str
    harness: Harness | None = None
    error: str | None = None
    manifest: HarnessManifest | None = None


__all__ = ["LOCAL", "SHIPPED", "LoadedPlugin", "PluginError"]
