"""Claude Code built-in harness entrypoint."""

import json  # noqa: F401

from theater.harness.builtin.adapters.claude.launch import ClaudeCodeHarness
from theater.harness.builtin.adapters.claude.observer import ClaudeCodeObserver
from theater.harness.builtin.adapters.claude.usage import _claude_trajectory_usage, _token_usage

HARNESS = ClaudeCodeHarness()

__all__ = [
    "HARNESS",
    "ClaudeCodeHarness",
    "ClaudeCodeObserver",
    "_claude_trajectory_usage",
    "_token_usage",
]
