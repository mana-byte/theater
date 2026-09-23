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


class InspectorTab(StrEnum):
    SUMMARY = "summary"
    OUTPUT = "output"
    REASONING = "reasoning"
    USAGE = "usage"
    INPUT = "input"
    RESULT = "result"
    PREVIEW = "preview"
    RAW = "raw"
    SOURCE = "source"
    PAYLOAD = "payload"
    CURRENT = "current"
    PREVIOUS = "previous"
    DIFF = "diff"
    TIMING = "timing"
    ASSOCIATIONS = "associations"


__all__ = [
    "FocusRegion",
    "InspectorTab",
    "TimelineLane",
]
