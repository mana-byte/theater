"""Presentation-only trajectory enums."""

from __future__ import annotations

from enum import StrEnum


class OrderMode(StrEnum):
    ORDER = "order"
    DURATION = "duration"


class DiagnosticView(StrEnum):
    ALL = "all"
    RUNNING = "running"
    ERRORS = "errors"
    SLOW = "slow"
    TOOLS = "tools"
    WATERFALL = "waterfall"
    FILES = "files"
    RESOURCES = "resources"
    DELEGATION = "delegation"
    COORDINATION = "coordination"


class FocusRegion(StrEnum):
    TIMELINE = "timeline"
    LEDGER = "ledger"
    INSIGHTS = "insights"
    DETAIL = "detail"


class FilterDimension(StrEnum):
    LANE = "lane"
    KIND = "kind"
    STATUS = "status"
    SOURCE = "source"


class InspectorTab(StrEnum):
    SUMMARY = "summary"
    OUTPUT = "output"
    REASONING = "reasoning"
    USAGE = "usage"
    TIMING = "timing"
    INPUT = "input"
    RESULT = "result"
    PREVIEW = "preview"
    RAW = "raw"
    SOURCE = "source"
    PAYLOAD = "payload"
    CURRENT = "current"
    PREVIOUS = "previous"
    DIFF = "diff"
    ASSOCIATIONS = "associations"


INSIGHT_VIEWS = frozenset(
    {
        DiagnosticView.ERRORS,
        DiagnosticView.WATERFALL,
        DiagnosticView.FILES,
        DiagnosticView.RESOURCES,
        DiagnosticView.DELEGATION,
    }
)


__all__ = [
    "INSIGHT_VIEWS",
    "DiagnosticView",
    "FilterDimension",
    "FocusRegion",
    "InspectorTab",
    "OrderMode",
]
