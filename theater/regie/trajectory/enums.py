"""Wire and presentation enums used by trajectory components."""

from __future__ import annotations

from enum import Enum, StrEnum


class _UnknownEnumMixin:
    @classmethod
    def _missing_(cls, value: object) -> Enum | None:
        if isinstance(value, str):
            return getattr(cls, "__members__", {}).get("UNKNOWN")
        return None


class Lane(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    INPUT = "input"
    MODEL = "model"
    TOOLS = "tools"
    THEATER = "theater"


class RecordKind(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    TURN = "turn"
    STEP = "step"
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    CONTEXT_CHANGE = "context_change"
    SPAWN = "spawn"
    RESUME = "resume"
    SEND = "send"
    RECEIVE = "receive"
    AWAIT_START = "await_start"
    AWAIT_END = "await_end"
    KILL = "kill"
    JOB_FAILURE = "job_failure"
    SESSION_BOUNDARY = "session_boundary"
    OBSERVATION_ERROR = "observation_error"


class RecordStatus(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


class ContentFormat(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    TEXT = "text"
    MARKDOWN = "markdown"
    JSON = "json"
    DIFF = "diff"
    RAW = "raw"
    IMAGE = "image"
    BINARY = "binary"


class TimingProvenance(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    EXACT = "exact"
    SOURCE = "source"
    MISSING = "missing"


class LinkDirection(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    FROM = "from"
    TO = "to"
    RELATED = "related"


class PanelStatus(_UnknownEnumMixin, StrEnum):
    UNKNOWN = "unknown"
    LIVE = "live"
    DEAD = "dead"
    EXTERNAL = "external"
    MISSING = "missing"
    READY = "ready"
    LOADING = "loading"
    WAITING = "waiting"
    UNTRUSTED = "untrusted"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class OrderMode(StrEnum):
    ORDER = "order"
    DURATION = "duration"


class FocusRegion(StrEnum):
    TIMELINE = "timeline"
    LEDGER = "ledger"
    INSPECTOR = "inspector"


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


__all__ = [
    "ContentFormat",
    "FilterDimension",
    "FocusRegion",
    "InspectorTab",
    "Lane",
    "LinkDirection",
    "OrderMode",
    "PanelStatus",
    "RecordKind",
    "RecordStatus",
    "TimingProvenance",
]
