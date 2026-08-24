"""Presentation-only trajectory enums."""

from __future__ import annotations

from enum import StrEnum


class OrderMode(StrEnum):
    ORDER = "order"
    DURATION = "duration"


class FocusRegion(StrEnum):
    TIMELINE = "timeline"
    LEDGER = "ledger"


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


__all__ = ["FilterDimension", "FocusRegion", "InspectorTab", "OrderMode"]
