"""Compatibility façade: re-exports ``theater.harness.contracts`` under the old path.

Names are the very same objects; non-contract imports (e.g. ``shutil``) are not re-exported.
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
    TurnTerminal,
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
    "TurnTerminal",
    "clip",
    "clipper",
    "last_screen_line",
    "status_after",
    "theater_binary",
    "whole",
]
