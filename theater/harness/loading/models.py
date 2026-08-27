"""Result values for the package manifest loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from theater.harness.contracts.harness import Harness

#: Where a plugin came from.
SHIPPED = "shipped"
LOCAL = "local"


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """One discovered plugin directory, loaded or not."""

    path: Path
    source: str
    name: str
    harness: Harness | None = None
    error: str | None = None


__all__ = ["LOCAL", "SHIPPED", "LoadedPlugin"]
