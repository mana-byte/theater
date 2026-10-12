"""Which config changes a running daemon may take without a restart.

Hot keys are read by their policy check each time it runs; everything else is captured at start,
imported, or would retarget admitted work, so it stays restart-only — new fields included.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType

from theater.config.models import MODELS_SECTION, REASONING_SECTION, Config

HOT_KEYS = frozenset({"rails.depth_cap", "rails.budget", "skills.disabled"})
HOT_TABLES = frozenset({MODELS_SECTION, REASONING_SECTION})

#: Settings the daemon never reads, each with the process that reads it from the file.
OUTSIDE_DAEMON: Mapping[str, str] = MappingProxyType(
    {
        "theater.favourite": "each `theater` command reads it",
        "observability.mcp_timing": "each new Theater MCP process reads it",
    }
)

_PROVENANCE = frozenset({"sources", "path", "exists"})
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


def effective_values(config: Config) -> dict[str, object]:
    """Every setting by dotted key, tables expanded one level; provenance is not a setting."""
    values: dict[str, object] = {}
    for item in fields(config):
        if item.name not in _PROVENANCE:
            _flatten(item.name, getattr(config, item.name), values)
    return values


def _flatten(prefix: str, value: object, values: dict[str, object]) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        for member in fields(value):
            _flatten(f"{prefix}.{member.name}", getattr(value, member.name), values)
    elif isinstance(value, Mapping):
        for key, entry in value.items():
            values[f"{prefix}.{key}"] = entry
    else:
        values[prefix] = value


def compare(active: Config, candidate: Config) -> ConfigChanges:
    """Classify every setting whose effective value differs between the two configs."""
    before, after = effective_values(active), effective_values(candidate)
    hot: list[str] = []
    restart: list[str] = []
    outside: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        if before.get(key, _ABSENT) == after.get(key, _ABSENT):
            continue
        if key in OUTSIDE_DAEMON:
            outside.append(key)
        elif key in HOT_KEYS or key.split(".", 1)[0] in HOT_TABLES:
            hot.append(key)
        else:
            restart.append(key)
    return ConfigChanges(tuple(hot), tuple(restart), tuple(outside))
