"""Trajectory enum values and validation error."""

from __future__ import annotations

from enum import StrEnum


class TrajectoryValidationError(ValueError):
    """A trajectory value or wire object failed validation."""


class TrajectoryLane(StrEnum):
    INPUT = "input"
    MODEL = "model"
    TOOLS = "tools"
    THEATER = "theater"


class TrajectoryKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    SYSTEM = "system"
    CONTEXT = "context"
    THEATER = "theater"
    SPAWN = "spawn"
    RESUME = "resume"
    SEND = "send"
    RECEIVE = "receive"
    AWAIT_START = "await_start"
    AWAIT_END = "await_end"
    KILL = "kill"
    JOB_FAILURE = "job_failure"
    TRANSCRIPT_BOUNDARY = "transcript_boundary"
    SESSION_BOUNDARY = "session_boundary"
    OBSERVATION_ERROR = "observation_error"
    UNKNOWN = "unknown"


class TrajectoryStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class ContentFormat(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    JSON = "json"
    CODE = "code"
    DIFF = "diff"
    PATH = "path"
    IMAGE = "image"
    BINARY = "binary"


class TimingProvenance(StrEnum):
    SOURCE = "source"
    OBSERVED = "observed"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


class CostProvenance(StrEnum):
    UNKNOWN = "unknown"
    REPORTED = "reported"
    ESTIMATED = "estimated"


class TrajectoryFailureCategory(StrEnum):
    PROVIDER = "provider"
    TOOL = "tool"
    USER = "user"
    THEATER = "theater"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    INCOMPLETE_TRANSCRIPT = "incomplete_transcript"
    UNKNOWN = "unknown"


class LinkDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    RELATED = "related"


class GroupKind(StrEnum):
    TURN = "turn"
    STEP = "step"
    BETWEEN_TURNS = "between_turns"


class PanelState(StrEnum):
    READY = "ready"
    WAITING = "waiting"
    UNTRUSTED = "untrusted"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class TrajectoryParticipantState(StrEnum):
    LIVE = "live"
    DEAD = "dead"
    EXTERNAL = "external"
    MISSING = "missing"
    UNKNOWN = "unknown"


__all__ = [
    "ContentFormat",
    "CostProvenance",
    "GroupKind",
    "LinkDirection",
    "PanelState",
    "TimingProvenance",
    "TrajectoryFailureCategory",
    "TrajectoryKind",
    "TrajectoryLane",
    "TrajectoryParticipantState",
    "TrajectoryStatus",
    "TrajectoryValidationError",
]
