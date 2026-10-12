"""Which config changes a running daemon may take without a restart.

Hot keys are read by their policy check each time it runs; everything else is captured at start,
imported, or would retarget admitted work, so it stays restart-only — new fields included.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType

from theater.config.models import MODELS_SECTION, REASONING_SECTION, Config

HOT_KEYS = frozenset({"rails.depth_cap", "rails.budget", "skills.disabled"})
HOT_TABLES = frozenset({MODELS_SECTION, REASONING_SECTION})

#: Settings the daemon never reads, each with what does read it from the file.
OUTSIDE_DAEMON: Mapping[str, str] = MappingProxyType(
    {
        "theater.favourite": (
            "read by `theater spawn` when no harness is named, and by Régie at launch"
        ),
        "observability.mcp_timing": "read by each new Theater MCP process",
    }
)

_PROVENANCE = frozenset({"sources", "path", "exists"})
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ABSENT = object()


@dataclass(frozen=True, slots=True)
class ConfigChanges:
    """Dotted keys whose effective value differs, sorted, by what applying them needs."""

    hot: tuple[str, ...] = ()
    restart: tuple[str, ...] = ()
    outside_daemon: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.hot or self.restart or self.outside_daemon)


def effective_values(config: Config) -> dict[tuple[str, ...], object]:
    """Every setting by key path, maps expanded down to their leaves; a list is one value.
    The loader folds an absent and an empty section table into one default, so those never differ;
    below them an empty map is kept as a value, since a plugin is compiled with ``None`` for an
    absent table but ``{}`` for an empty one. Provenance is not a setting.
    """
    values: dict[tuple[str, ...], object] = {}
    for item in fields(config):
        if item.name not in _PROVENANCE:
            _flatten((item.name,), getattr(config, item.name), values, nested=False)
    return values


def _flatten(
    path: tuple[str, ...], value: object, values: dict[tuple[str, ...], object], *, nested: bool
) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        for member in fields(value):
            _flatten((*path, member.name), getattr(value, member.name), values, nested=False)
    elif isinstance(value, Mapping):
        if nested and not value:
            values[path] = {}
        for key, entry in value.items():
            _flatten((*path, str(key)), entry, values, nested=True)
    else:
        values[path] = value


def _same(left: object, right: object) -> bool:
    """Equality that keeps TOML types apart: ``1``, ``1.0`` and ``true`` are three values."""
    if type(left) is not type(right):
        return False
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return len(left) == len(right) and all(map(_same, left, right))
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, float) and isinstance(right, float):
        return left == right or (math.isnan(left) and math.isnan(right))
    return left == right


def _dotted(path: tuple[str, ...]) -> str:
    """The TOML spelling of a key path: a segment that is not a bare key is quoted."""
    return ".".join(part if _BARE_KEY.fullmatch(part) else json.dumps(part) for part in path)


def compare(active: Config, candidate: Config) -> ConfigChanges:
    """Classify every setting whose effective value differs between the two configs."""
    before, after = effective_values(active), effective_values(candidate)
    hot: list[str] = []
    restart: list[str] = []
    outside: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        if _same(before.get(path, _ABSENT), after.get(path, _ABSENT)):
            continue
        key = _dotted(path)
        if key in OUTSIDE_DAEMON:
            outside.append(key)
        elif key in HOT_KEYS or (len(path) == 2 and path[0] in HOT_TABLES):
            hot.append(key)
        else:
            restart.append(key)
    return ConfigChanges(tuple(hot), tuple(restart), tuple(outside))
