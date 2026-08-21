"""Centralized mutable registry state.

Each object here is a single instance, mutated in place by ``install`` and
read by identity by every other module.  Rebinding any of them would leave
holders reading a stale registry with no symptom but a missing harness.
"""

from __future__ import annotations

from theater.harness.contracts.harness import Harness
from theater.harness.plugins import Plugin

#: The live registry. Mutated in place by ``install``, never rebound.
HARNESSES: dict[str, Harness] = {}

#: Aliases a misreporting agent might send, mapped to the canonical name.
_ALIASES: dict[str, str] = {}

#: name -> the plugin file it came from. For collision messages and SOURCE column.
_PLUGINS: dict[str, Plugin] = {}

#: Binary names claimed by registered harnesses -> (harness name, claimant path).
_BINARIES: dict[str, tuple[str, str]] = {}

#: Tmux observation keys (15-char truncated binary names) -> (harness name, path).
_OBSERVATION_KEYS: dict[str, tuple[str, str]] = {}

#: Local plugins that would not load, listed by ``theater harnesses`` as broken.
_BROKEN: list[Plugin] = []
