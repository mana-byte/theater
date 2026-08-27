"""Claude record and request timing projection."""

from __future__ import annotations

from dataclasses import dataclass

from theater.harness.normalization.timing import epoch_or_number as _trajectory_time
from theater.harness.normalization.values import finite_float as _trajectory_float
from theater.harness.normalization.values import trajectory_identifier as _trajectory_id
from theater.trajectory.enums import TimingProvenance
from theater.trajectory.records import Timing


@dataclass(frozen=True, slots=True)
class _ClaudeCausalRecord:
    timestamp: float | None
    turn_id: str | None


@dataclass(frozen=True, slots=True)
class _ClaudeRequestClock:
    start: float | None
    first_token: float | None


@dataclass(frozen=True, slots=True)
class _ClaudeTimingProjection:
    record: Timing | None
    request: Timing | None
    turn_id: str | None


def _trajectory_duration(record: dict) -> float | None:
    for key in ("durationMs", "duration_ms"):
        value = _trajectory_float(record.get(key))
        if value is not None and value >= 0:
            return value
    return None


def _claude_timing(record: dict, timestamp: float | None) -> Timing | None:
    start = next(
        (
            _trajectory_time(record.get(key))
            for key in ("startTimestamp", "start_timestamp", "startedAt", "started_at")
            if _trajectory_time(record.get(key)) is not None
        ),
        None,
    )
    end = next(
        (
            _trajectory_time(record.get(key))
            for key in ("endTimestamp", "end_timestamp", "completedAt", "completed_at")
            if _trajectory_time(record.get(key)) is not None
        ),
        None,
    )
    if start is None and timestamp is not None:
        start = timestamp
    duration = _trajectory_duration(record)
    if start is None and end is None and duration is None:
        return None
    if start is not None and end is not None and end < start:
        end = None
    return Timing(start=start, end=end, duration_ms=duration, provenance=TimingProvenance.SOURCE)


def _claude_turn_timing(record: dict, timestamp: float | None) -> Timing | None:
    explicit = _claude_timing(record, None)
    start = explicit.start if explicit is not None else None
    end = explicit.end if explicit is not None else None
    duration = explicit.duration_ms if explicit is not None else None
    end = end if end is not None else timestamp
    derived = False
    if duration is not None:
        if start is None and end is not None:
            start = end - duration / 1_000
            derived = True
        elif end is None and start is not None:
            end = start + duration / 1_000
            derived = True
    elif start is not None and end is not None:
        duration = (end - start) * 1_000
        derived = True
    if start is not None and end is not None and end < start:
        start = None
    if start is None and end is None and duration is None:
        return None
    return Timing(
        start=start,
        end=end,
        duration_ms=duration,
        provenance=TimingProvenance.DERIVED if derived else TimingProvenance.SOURCE,
    )


def _claude_request_id(message: dict, record: dict) -> str | None:
    return _trajectory_id(
        record.get("requestId")
        or record.get("request_id")
        or message.get("requestId")
        or message.get("request_id")
        or message.get("id")
    )


def _claude_request_bounds(
    explicit: Timing | None,
    prior: _ClaudeRequestClock | None,
    timestamp: float | None,
    parent_timestamp: float | None,
) -> tuple[float | None, float | None, float | None]:
    start = explicit.start if explicit is not None else None
    end = explicit.end if explicit is not None else None
    duration = explicit.duration_ms if explicit is not None else None
    if duration is not None:
        if start is not None and end is None:
            end = start + duration / 1_000
        elif end is not None and start is None:
            start = end - duration / 1_000
        elif start is None and end is None and timestamp is not None:
            end = timestamp
            start = end - duration / 1_000
    if start is None and prior is not None:
        start = prior.start
    if start is None:
        start = parent_timestamp
    if end is None:
        end = timestamp
    if start is not None and end is not None and end < start:
        start = end = None
    return start, end, duration


def _claude_first_token(
    prior: _ClaudeRequestClock | None,
    timestamp: float | None,
    start: float | None,
    end: float | None,
) -> float | None:
    first_token = prior.first_token if prior is not None else timestamp
    if first_token is not None and start is not None and first_token < start:
        return None
    if first_token is not None and end is not None and first_token > end:
        return None
    return first_token


def _claude_request_timing_value(
    explicit: Timing | None,
    fallback: Timing | None,
    start: float | None,
    end: float | None,
    duration: float | None,
    first_token: float | None,
) -> Timing | None:
    if start is not None and end is not None:
        complete_source = (
            explicit is not None
            and explicit.start is not None
            and explicit.end is not None
            and explicit.duration_ms is not None
        )
        return Timing(
            start=start,
            end=end,
            duration_ms=duration if duration is not None else (end - start) * 1_000,
            provenance=(TimingProvenance.SOURCE if complete_source else TimingProvenance.DERIVED),
            first_token=first_token,
        )
    if duration is None:
        return fallback
    return Timing(
        start=start,
        end=end,
        duration_ms=duration,
        provenance=explicit.provenance if explicit is not None else TimingProvenance.SOURCE,
        first_token=first_token,
    )
