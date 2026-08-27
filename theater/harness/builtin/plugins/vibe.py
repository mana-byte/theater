"""Mistral Vibe plugin entrypoint."""

from __future__ import annotations

from theater.harness.builtin.adapters.vibe.constants import _MARKER_KEY as _VIBE_MARKER_KEY
from theater.harness.builtin.adapters.vibe.constants import ISOLATION_MARKER
from theater.harness.builtin.adapters.vibe.isolation import (
    isolation_marker_text,
    validate_isolated_domain,
)
from theater.harness.builtin.adapters.vibe.launch import VibeHarness
from theater.harness.builtin.adapters.vibe.observer import VibeObserver
from theater.harness.builtin.adapters.vibe.source import _VibeSource

__all__ = [
    "HARNESS",
    "ISOLATION_MARKER",
    "VibeHarness",
    "VibeObserver",
    "_VibeSource",
    "isolation_marker_text",
    "validate_isolated_domain",
]

_MARKER_KEY = _VIBE_MARKER_KEY
HARNESS = VibeHarness()
