"""Alias, binary, and tmux observation-key claims and release.

Collisions are refused at load time, naming both files, instead of resolved by iteration order.
"""

from __future__ import annotations

from theater.config import ConfigError
from theater.constants.harness import HARNESS_TMUX_OBSERVATION_NAME_LENGTH
from theater.harness.contracts.harness import Harness
from theater.harness.registry import (
    _ALIASES,
    _BINARIES,
    _OBSERVATION_KEYS,
    _PLUGINS,
    HARNESSES,
)


def unwrap_binary(binary: str) -> str:
    """Strip nixpkgs makeWrapper affixes from a binary name."""
    name = binary.rsplit("/", 1)[-1]
    if name.startswith("."):
        name = name[1:]
    if name.endswith("-wrapped"):
        name = name[: -len("-wrapped")]
    return name


def binary_claim_keys(binary: str) -> set[str]:
    """Every key ``match_binary`` could match ``binary`` under."""
    basename = binary.rsplit("/", 1)[-1]
    unwrapped = unwrap_binary(binary)
    keys = {binary, basename, unwrapped}
    keys.discard("")
    return keys


def claim_alias(alias: str, owner: str, claimant: str) -> None:
    """Point ``alias`` at ``owner``, unless something else already owns it.

    An alias collision is refused rather than resolved by load order;
    the error names both files so the user can find the conflict.
    """
    target = _ALIASES.get(alias)
    if target is not None and target != owner:
        prev_plugin = _PLUGINS.get(target)
        prev_path = str(prev_plugin.path) if prev_plugin is not None else "(unknown)"
        raise ConfigError(
            f"{claimant} claims alias {alias!r}, which already resolves to {target!r} ({prev_path})"
        )
    if alias in HARNESSES and alias != owner:
        prev_plugin = _PLUGINS.get(alias)
        prev_path = str(prev_plugin.path) if prev_plugin is not None else "(unknown)"
        raise ConfigError(
            f"{claimant} claims alias {alias!r}, which is the name of another harness ({prev_path})"
        )
    _ALIASES[alias] = owner


def claim_name(name: str, claimant: str) -> None:
    """Guard a primary name against an alias some earlier plugin already claimed.

    The mirror of ``claim_alias``'s second guard: a harness *name* that
    shadows an already-claimed alias is caught here.
    """
    owner = _ALIASES.get(name)
    if owner is not None and owner != name:
        raise ConfigError(
            f"{claimant} registers harness {name!r}, which is already an alias of {owner!r}"
        )


def claim_binary(binary: str, owner: str, claimant: str) -> None:
    """Claim a binary name for ``owner``; refuse if taken, since ``match_binary`` would pick by
    order.
    """
    for key in binary_claim_keys(binary):
        existing = _BINARIES.get(key)
        if existing is not None and existing[0] != owner:
            prev_owner, prev_path = existing
            raise ConfigError(
                f"{claimant} claims binary {key!r} for harness {owner!r}, which is "
                f"already claimed by harness {prev_owner!r} ({prev_path})"
            )
        _BINARIES[key] = (owner, claimant)


def _observation_keys_for(binary: str) -> set[str]:
    """Every 15-character observation key tmux could report for ``binary``.

    Covers raw, basename, unwrapped, and makeWrapper ``name-wrapped``/``.name-wrapped`` spellings.
    """
    basename = binary.rsplit("/", 1)[-1]
    unwrapped = unwrap_binary(binary)
    spellings: set[str] = {binary, basename, unwrapped}
    spellings.add(f"{unwrapped}-wrapped")
    spellings.add(f".{unwrapped}-wrapped")
    keys: set[str] = set()
    for spelling in spellings:
        base = spelling.rsplit("/", 1)[-1]
        if len(base) >= HARNESS_TMUX_OBSERVATION_NAME_LENGTH:
            keys.add(base[:HARNESS_TMUX_OBSERVATION_NAME_LENGTH])
    keys.discard("")
    return keys


def claim_observation_keys(binary: str, owner: str, claimant: str) -> None:
    """Claim every 15-character observation key for ``owner``; truncation collisions are refused."""
    for key in _observation_keys_for(binary):
        existing = _OBSERVATION_KEYS.get(key)
        if existing is not None and existing[0] != owner:
            prev_owner, prev_path = existing
            raise ConfigError(
                f"{claimant} claims observation key {key!r} for harness {owner!r} "
                f"(truncated from {binary!r}), which is already claimed by harness "
                f"{prev_owner!r} ({prev_path}) — tmux truncates pane_current_command "
                f"to 15 characters, so both binaries would appear identical in tmux"
            )
        _OBSERVATION_KEYS[key] = (owner, claimant)


def release_claims(harness: Harness, name: str) -> None:
    """Remove a superseded harness's binary and observation-key claims.

    Aliases stay: they resolve to the harness name, which the override shares.
    """
    for key in binary_claim_keys(harness.binary):
        existing = _BINARIES.get(key)
        if existing is not None and existing[0] == name:
            del _BINARIES[key]
    for extra in harness.binaries:
        for key in binary_claim_keys(extra):
            existing = _BINARIES.get(key)
            if existing is not None and existing[0] == name:
                del _BINARIES[key]
    for key in _observation_keys_for(harness.binary):
        existing = _OBSERVATION_KEYS.get(key)
        if existing is not None and existing[0] == name:
            del _OBSERVATION_KEYS[key]
    for extra in harness.binaries:
        for key in _observation_keys_for(extra):
            existing = _OBSERVATION_KEYS.get(key)
            if existing is not None and existing[0] == name:
                del _OBSERVATION_KEYS[key]
