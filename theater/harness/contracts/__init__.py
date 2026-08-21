"""Canonical harness contract modules.

Event types, launch-plan dataclasses, the Harness ABC, and observation
contracts live here. ``base.py`` re-exports everything in this package for
backward compatibility.
"""

from __future__ import annotations

from theater.harness.contracts.events import (
    Event,
    EventKind,
    EventPath,
    TokenUsage,
    clip,
    clipper,
    last_screen_line,
    status_after,
    whole,
)
from theater.harness.contracts.harness import APPROVALS, Harness
from theater.harness.contracts.launch import (
    LaunchPlan,
    NativeChild,
    ResumeLaunchOverlay,
    theater_binary,
)
from theater.harness.contracts.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)

__all__ = [
    "APPROVALS",
    "Event",
    "EventKind",
    "EventPath",
    "Harness",
    "HarnessObserver",
    "LaunchPlan",
    "NativeChild",
    "ResumeLaunchOverlay",
    "ScreenConfidence",
    "ScreenKind",
    "ScreenReading",
    "TokenUsage",
    "clip",
    "clipper",
    "last_screen_line",
    "status_after",
    "theater_binary",
    "whole",
]
