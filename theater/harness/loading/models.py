"""Result values and error types for the package manifest loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from theater.config import ConfigError
from theater.harness.contracts.harness import Harness

#: Where a plugin came from.
SHIPPED = "shipped"
LOCAL = "local"


class PluginError(ConfigError):
    """A plugin manifest that cannot be turned into a harness."""


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """One discovered plugin, loaded or not.

    The path travels with the harness because the registry needs it for the
    collision messages: "two definitions of `codex`" is only actionable if it
    says which two. `source` travels with it for the same reason.

    `name` is the canonical directory name, which is also the harness name. A
    plugin that raises on import still has one, and the whole point of
    `[harness] disabled` is to be able to switch off the one breaking start-up.
    """

    path: Path
    source: str
    name: str
    harness: Harness | None = None
    error: str | None = None


__all__ = ["LOCAL", "SHIPPED", "LoadedPlugin", "PluginError"]
