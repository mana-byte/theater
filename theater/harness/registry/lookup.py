"""Registry lookup: normalize, get, observation_lookup, icon, describe, known binaries."""

from __future__ import annotations

import shutil

from theater.harness.contracts.harness import Harness
from theater.harness.registry import (
    _ALIASES,
    _BROKEN,
    _OBSERVATION_KEYS,
    _PLUGINS,
    HARNESSES,
)
from theater.models import BadRequest

#: Shown for a participant whose harness has no adapter.
UNKNOWN_ICON = "?"


def normalize(name: str) -> str:
    """Map a harness name as an agent might report it to the canonical key.

    Unknown names are returned unchanged so the caller can decide whether to
    reject or accept as-is.
    """
    return _ALIASES.get(name, name)


def get(name: str) -> Harness:
    """Return the harness for ``name``, or raise ``BadRequest``."""
    harness = HARNESSES.get(name)
    if harness is None:
        known = ", ".join(sorted(HARNESSES))
        raise BadRequest(f"unknown harness {name!r}; known: {known}")
    return harness


def observation_lookup(key: str) -> str | None:
    """Resolve a 15-character observation to a harness name, or None.

    Called by ``match_binary`` when the observed basename is exactly 15
    characters long — the truncation length shared by tmux's
    ``pane_current_command`` and Linux's ``/proc/<pid>/comm``.
    """
    entry = _OBSERVATION_KEYS.get(key)
    return entry[0] if entry is not None else None


def harness_icon(name: str | None) -> str:
    """The one-character mark for a harness name.

    Normalizes first so aliases receive their canonical glyph. Unknown names
    are not an error here.
    """
    harness = HARNESSES.get(normalize(name or ""))
    return harness.icon if harness else UNKNOWN_ICON


def describe() -> list[dict]:
    """Every registered harness as plain data, sorted by name.

    One builder for three consumers — the ``harnesses`` RPC, ``theater
    harnesses`` and the régie's palette.  ``installed`` is resolved here, so
    it describes the PATH of whichever process called.  Broken local plugins
    come last, with ``error`` set and no usable binary.
    """
    rows = []
    for name in sorted(HARNESSES):
        harness = HARNESSES[name]
        path = shutil.which(harness.binary)
        rows.append(
            {
                "name": name,
                "icon": harness.icon,
                "binary": harness.binary,
                "installed": path is not None,
                "path": path,
                "source": _PLUGINS[name].source,
                "error": None,
            }
        )
    for found in sorted(_BROKEN, key=lambda p: p.name):
        rows.append(
            {
                "name": found.name,
                "icon": UNKNOWN_ICON,
                "binary": "",
                "installed": False,
                "path": str(found.path),
                "source": found.source,
                "error": found.error,
            }
        )
    return rows


def known_binaries() -> set[str]:
    """Every binary name the registered harnesses look for on PATH.

    Used by the unmanaged-pane sweep. Includes plugin-declared ``binaries``
    aliases when set.
    """
    result: set[str] = set()
    for h in HARNESSES.values():
        result.add(h.binary)
        result |= h.binaries
    return result
