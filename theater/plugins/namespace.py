"""The global canonical-name namespace shared by plugin kinds."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PluginNameReservation:
    """One discovered canonical package name, whether or not it loaded."""

    kind: str
    name: str
    path: Path
    source: str


class NamespaceCollision(ValueError):
    """Two different plugin kinds claimed one canonical package name."""


def reject_cross_kind_collisions(
    first: Iterable[PluginNameReservation],
    second: Iterable[PluginNameReservation],
) -> None:
    """Reject canonical names that appear in both independent type registries."""
    first_by_name = _by_name(first)
    second_by_name = _by_name(second)
    for name in sorted(first_by_name.keys() & second_by_name.keys()):
        left = first_by_name[name]
        right = second_by_name[name]
        raise NamespaceCollision(
            f"{right.path} declares {right.kind} plugin {name!r}, which conflicts with "
            f"{left.kind} plugin at {left.path}; canonical plugin names are shared across kinds"
        )


def _by_name(values: Iterable[PluginNameReservation]) -> dict[str, PluginNameReservation]:
    result: dict[str, PluginNameReservation] = {}
    for value in values:
        result.setdefault(value.name, value)
    return result
