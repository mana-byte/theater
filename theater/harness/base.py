"""Compatibility façade re-exporting the former contract definitions.

The canonical definitions now live in ``theater.harness.contracts``. This
module preserves the contract symbols and import paths that previously
targeted ``theater.harness.base``. Object identity is unchanged: every
name below is the very object defined in contracts. Implementation imports
that were never part of the contract (e.g. ``shutil``) are intentionally
not re-exported here.
"""

from __future__ import annotations

from theater.constants.harness import (
    HARNESS_APPROVAL_POLICIES as APPROVALS,
)
from theater.constants.harness import (
    HARNESS_EVENT_TEXT_MAX_CHARS as MAX_TEXT,
)
from theater.constants.harness import (
    HARNESS_MCP_SERVER_NAME as SERVER_NAME,
)
from theater.constants.harness import (
    HARNESS_MCP_TOOL_TIMEOUT_SECONDS as MCP_TOOL_TIMEOUT,
)
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
from theater.harness.contracts.harness import Harness, LaunchParameterSupport
from theater.harness.contracts.launch import (
    ChannelCredential,
    LaunchPlan,
    NativeChild,
    ResumeLaunchOverlay,
    theater_binary,
)

__all__ = [
    "APPROVALS",
    "MAX_TEXT",
    "MCP_TOOL_TIMEOUT",
    "SERVER_NAME",
    "ChannelCredential",
    "Event",
    "EventKind",
    "EventPath",
    "Harness",
    "LaunchParameterSupport",
    "LaunchPlan",
    "NativeChild",
    "ResumeLaunchOverlay",
    "TokenUsage",
    "clip",
    "clipper",
    "last_screen_line",
    "status_after",
    "theater_binary",
    "whole",
]
