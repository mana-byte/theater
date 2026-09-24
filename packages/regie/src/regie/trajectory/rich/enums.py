"""Presentation-only trajectory enums."""

from __future__ import annotations

from enum import StrEnum


class TimelineLane(StrEnum):
    INPUT = "input"
    MODEL = "model"
    TOOLS = "tools"
    MCP = "mcp"
    THEATER = "theater"


class FocusRegion(StrEnum):
    TIMELINE = "timeline"
    DETAIL = "detail"


__all__ = [
    "FocusRegion",
    "TimelineLane",
]
