"""OpenCode built-in harness entrypoint."""

from theater.harness.builtin.adapters.opencode.launch import OpenCodeHarness
from theater.harness.builtin.adapters.opencode.observer import OpenCodeObserver
from theater.harness.builtin.adapters.opencode.paths import (
    _paths_from_tool,
    _relativise,
)
from theater.harness.builtin.adapters.opencode.source import OpenCodeSource
from theater.harness.builtin.adapters.opencode.values import (
    _opencode_usage,
    _trajectory_usage,
)

HARNESS = OpenCodeHarness()

__all__ = (
    "HARNESS",
    "OpenCodeHarness",
    "OpenCodeObserver",
    "OpenCodeSource",
    "_opencode_usage",
    "_paths_from_tool",
    "_relativise",
    "_trajectory_usage",
)
