"""Parse a play's PLAY.toml into immutable, validated rules; data only, nothing executes."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping

from theater.constants.core import HARNESS_NAME
from theater.constants.skills import (
    PLAY_MANIFEST_FILENAME,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_MAX_COUNT,
    SKILL_NAME,
    SKILL_NAME_MAX_CHARS,
)
from theater.skills.models import PlayRules, SkillValidationError

_REQUIRED = frozenset({"description", "master", "positions"})
_ALLOWED = _REQUIRED | {"roles", "disposable", "plugins", "plugin_permissions"}


def parse_play_manifest(text: str, *, play: str) -> tuple[str, PlayRules]:
    """Return the play's description and rules, or raise SkillValidationError."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(f"is not valid TOML: {exc}") from exc
    keys = set(data)
    if not keys >= _REQUIRED or not keys <= _ALLOWED:
        raise _invalid(
            "must contain description, master, positions, and optionally roles, "
            "disposable, plugins, and plugin_permissions"
        )
    description = data["description"]
    if (
        not isinstance(description, str)
        or not description.strip()
        or not description.isprintable()
        or len(description) > SKILL_DESCRIPTION_MAX_CHARS
    ):
        raise _invalid(
            f"description must be printable text of 1-{SKILL_DESCRIPTION_MAX_CHARS} characters"
        )
    master = _name(data["master"], "master")
    positions = _table(data["positions"], "positions")
    roles = _table(data.get("roles", {}), "roles")
    if master not in positions:
        raise _invalid(f"master {master!r} must be listed under [positions]")
    if overlap := sorted(set(positions) & set(roles)):
        raise _invalid(f"{', '.join(overlap)} cannot be both a position and a role")
    if len(positions) + len(roles) > SKILL_MAX_COUNT:
        raise _invalid(f"may declare at most {SKILL_MAX_COUNT} positions and roles")
    for position, children in positions.items():
        if master in children:
            raise _invalid(f"{position!r} cannot spawn the master {master!r}")
        if unknown := sorted(set(children) - set(positions)):
            raise _invalid(f"{position!r} spawns undeclared positions: {', '.join(unknown)}")
    for role, fits in roles.items():
        if not fits:
            raise _invalid(f"role {role!r} must name the positions it fits")
        if unknown := sorted(set(fits) - (set(positions) - {master})):
            raise _invalid(f"role {role!r} fits unknown or master positions: {', '.join(unknown)}")
    _require_tree(master, positions)
    disposable = _disposable(data.get("disposable", []), master, positions)
    plugins, permissions = _plugin_rules(data, positions)
    return description, PlayRules(
        play=play,
        master=master,
        positions=positions,
        roles=roles,
        disposable=disposable,
        plugins=plugins,
        plugin_permissions=permissions,
    )


def _plugin_rules(
    data: dict, positions: Mapping[str, tuple[str, ...]]
) -> tuple[tuple[str, ...], dict[str, dict[str, tuple[str, ...]]]]:
    raw = data.get("plugins", [])
    if not isinstance(raw, list) or len(raw) > SKILL_MAX_COUNT:
        raise _invalid("plugins must be a bounded list of canonical plugin names")
    for name in raw:
        if not isinstance(name, str) or HARNESS_NAME.fullmatch(name) is None:
            raise _invalid(f"plugins contains invalid plugin name {name!r}")
    if len(set(raw)) != len(raw):
        raise _invalid("plugins repeats a name")
    tables = data.get("plugin_permissions", {})
    if not isinstance(tables, dict):
        raise _invalid("plugin_permissions must be a table keyed by plugin name")
    permissions: dict[str, dict[str, tuple[str, ...]]] = {}
    for plugin, grants in tables.items():
        if plugin not in raw:
            raise _invalid(f"plugin_permissions.{plugin} requires a declared plugin dependency")
        parsed = _table(grants, f"plugin_permissions.{plugin}")
        if unknown := sorted(set(parsed) - set(positions)):
            raise _invalid(f"plugin_permissions.{plugin} names undeclared positions: {unknown}")
        permissions[plugin] = parsed
    return tuple(raw), permissions


def _disposable(
    value: object, master: str, positions: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """Positions an agent may retire without asking the user: declared, never the master."""
    if not isinstance(value, list):
        raise _invalid("disposable must be a list of position names")
    names = tuple(_name(item, "disposable") for item in value)
    if len(set(names)) != len(names):
        raise _invalid("disposable repeats a name")
    if unknown := sorted(set(names) - set(positions)):
        raise _invalid(f"disposable names undeclared positions: {', '.join(unknown)}")
    if master in names:
        raise _invalid(f"disposable cannot include the master {master!r}")
    return names


def _require_tree(master: str, positions: Mapping[str, tuple[str, ...]]) -> None:
    """Every position is reachable from the master, and no position can respawn its ancestors."""
    visiting: set[str] = set()
    reached: set[str] = set()

    def visit(position: str) -> None:
        if position in visiting:
            raise _invalid(f"positions form a cycle through {position!r}")
        if position in reached:
            return
        visiting.add(position)
        for child in positions[position]:
            visit(child)
        visiting.discard(position)
        reached.add(position)

    visit(master)
    if unreachable := sorted(set(positions) - reached):
        raise _invalid(f"positions unreachable from the master: {', '.join(unreachable)}")


def _table(value: object, field: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise _invalid(f"[{field}] must be a table of name = [names]")
    result: dict[str, tuple[str, ...]] = {}
    for key, items in value.items():
        name = _name(key, field)
        if not isinstance(items, list):
            raise _invalid(f"{field}.{name} must be a list of names")
        names = tuple(_name(item, f"{field}.{name}") for item in items)
        if len(set(names)) != len(names):
            raise _invalid(f"{field}.{name} repeats a name")
        result[name] = names
    return result


def _name(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > SKILL_NAME_MAX_CHARS
        or SKILL_NAME.fullmatch(value) is None
    ):
        raise _invalid(f"{field} entries must be canonical member names; got {value!r}")
    return value


def _invalid(detail: str) -> SkillValidationError:
    return SkillValidationError(f"{PLAY_MANIFEST_FILENAME} {detail}")


__all__ = ["parse_play_manifest"]
